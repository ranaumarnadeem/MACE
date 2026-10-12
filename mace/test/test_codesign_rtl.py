"""Tier-0 tests for mace.codesign.rtl.

Run:
    pytest mace/test/test_codesign_rtl.py -q

The edit tests run against a synthetic checkout built from the catalog's own
templates. Whether each template matches the real RTL is checked against a
patched OpenPiton checkout in mace/test/cluster/test_rtl_catalog.py.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from mace.codesign import rtl
from mace.codesign.rtl import CATALOG, Edit, RtlDesign, RtlParam, RtlSpace, edits_for, parse_design


def synthetic_checkout() -> dict[str, str]:
    """Every catalog file, holding each of its edits' text at the default value, one per line."""
    files: dict[str, list[str]] = {}
    for param in CATALOG.values():
        for edit in param.edits:
            files.setdefault(edit.path, []).append(edit.text(param.default))
    return {path: "// header\n" + "\n".join(lines) + "\n// trailer\n" for path, lines in files.items()}


class TestCatalog:
    def test_every_default_is_one_of_its_values(self):
        for param in CATALOG.values():
            assert param.default in param.values

    def test_an_int_parameter_value_is_a_power_of_two(self):
        """The buffers' pointers wrap at a power of two."""
        for param in CATALOG.values():
            for value in param.values:
                if isinstance(value, int):
                    assert value & (value - 1) == 0, (param.name, value)

    def test_a_parameter_outside_the_design_tree_is_refused(self):
        with pytest.raises(ValueError, match="not under piton/design/"):
            RtlParam("x", "d", 4, (4, 8), (Edit("piton/verif/env/x.v", "N = {value}"),))

    def test_a_default_outside_the_values_is_refused(self):
        with pytest.raises(ValueError, match="default"):
            RtlParam("x", "d", 3, (4, 8), (Edit("piton/design/x.v", "N = {value}"),))

    def test_log2_of_a_value_that_is_not_a_power_of_two_is_refused(self):
        with pytest.raises(ValueError, match="power of two"):
            Edit("piton/design/x.v", "W = {log2}").text(6)

    def test_no_edit_targets_a_generated_file(self):
        """Every build overwrites .tmp.h and .tmp.v, so an edit there is lost."""
        for param in CATALOG.values():
            for edit in param.edits:
                assert ".tmp." not in edit.path, (param.name, edit.path)

    def test_a_generated_check_names_a_tmp_file(self):
        for param in CATALOG.values():
            for edit in param.edits:
                if edit.generated:
                    assert ".tmp." in edit.generated and edit.path.endswith(".pyv")


class TestEdits:
    def test_the_default_design_changes_nothing(self):
        assert edits_for(RtlDesign.of({}), synthetic_checkout().__getitem__) == {}

    @pytest.mark.parametrize(
        "name,value",
        [(n, v) for n, p in CATALOG.items() for v in p.values if v != p.default],
    )
    def test_a_parameter_changes_its_own_text_and_nothing_else(self, name, value):
        checkout = synthetic_checkout()
        changed = edits_for(RtlDesign.of({name: value}), checkout.__getitem__)
        param = CATALOG[name]
        assert set(changed) == {e.path for e in param.edits}
        for edit in param.edits:
            assert edit.text(value) in changed[edit.path]
            assert edit.text(param.default) not in changed[edit.path]
        for path, text in changed.items():
            assert text != checkout[path]
            # Only the edited lines differ.
            before, after = checkout[path].splitlines(), text.splitlines()
            assert len(before) == len(after)
            assert sum(b != a for b, a in zip(before, after)) == sum(1 for e in param.edits if e.path == path)

    def test_the_log_width_follows_the_value(self):
        changed = edits_for(RtlDesign.of({"l2_mshr": 16}), synthetic_checkout().__getitem__)
        header = changed["piton/design/include/l2.h.pyv"]
        assert "`define L2_MSHR_ENTRIES         16" in header
        assert "`define L2_MSHR_INDEX_WIDTH     4" in header

    def test_two_parameters_in_one_file_both_land(self):
        design = RtlDesign.of({"store_buffer": 8, "scoreboard": 16})
        changed = edits_for(design, synthetic_checkout().__getitem__)
        text = changed["piton/design/chip/tile/ariane/core/include/ariane_pkg.sv"]
        assert "DEPTH_SPEC   = 8;" in text and "DEPTH_COMMIT = 8;" in text and "NR_SB_ENTRIES = 16;" in text

    def test_a_string_valued_parameter_is_written_by_name(self):
        changed = edits_for(RtlDesign.of({"home_alloc": "LOW"}), synthetic_checkout().__getitem__)
        (text,) = changed.values()
        assert "home_alloc_method <= `HOME_ALLOC_LOW_ORDER_BITS;" in text

    def test_old_text_that_is_missing_is_refused(self):
        checkout = synthetic_checkout()
        path = "piton/design/chip/tile/ariane/core/include/wt_cache_pkg.sv"
        checkout[path] = "// a different version of the file\n"
        with pytest.raises(ValueError, match="dcache_wbuf.*0 times"):
            edits_for(RtlDesign.of({"dcache_wbuf": 4}), checkout.__getitem__)

    def test_old_text_that_occurs_twice_is_refused(self):
        checkout = synthetic_checkout()
        path = "piton/design/chip/tile/ariane/core/include/wt_cache_pkg.sv"
        checkout[path] += checkout[path]
        with pytest.raises(ValueError, match="dcache_wbuf.*2 times"):
            edits_for(RtlDesign.of({"dcache_wbuf": 4}), checkout.__getitem__)

    def test_line_endings_survive(self, tmp_path):
        path = tmp_path / "piton/design/chip/tile/ariane/core/include/wt_cache_pkg.sv"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"a\r\n" + CATALOG["dcache_wbuf"].edits[0].text(8).encode() + b"\r\nb\r\n")
        changed = edits_for(RtlDesign.of({"dcache_wbuf": 4}), rtl.read_in(str(tmp_path)))
        (text,) = changed.values()
        assert text.count("\r\n") == 3

    def test_generated_checks_name_the_header_a_build_writes(self):
        checks = RtlDesign.of({"l2_mshr": 16, "dcache_wbuf": 4}).generated_checks()
        assert ("piton/design/include/l2.tmp.h", "`define L2_MSHR_ENTRIES         16") in checks
        assert all(path == "piton/design/include/l2.tmp.h" for path, _ in checks)

    def test_the_default_design_has_no_generated_checks(self):
        assert RtlDesign.of({}).generated_checks() == []


class TestStorage:
    def test_the_default_design_costs_nothing(self):
        assert RtlDesign.of({}).storage_delta() == 0

    def test_a_deeper_buffer_adds_bits_and_a_shallower_one_saves_them(self):
        assert RtlDesign.of({"dcache_wbuf": 16}).storage_delta() == 8 * 147
        assert RtlDesign.of({"dcache_wbuf": 4}).storage_delta() == -4 * 147

    def test_costs_add_across_parameters(self):
        both = RtlDesign.of({"dcache_wbuf": 16, "l2_mshr": 4})
        assert both.storage_delta() == 8 * 147 - 4 * 154

    def test_a_parameter_with_no_storage_adds_none(self):
        assert RtlDesign.of({"home_alloc": "HIGH"}).storage_delta() == 0


class TestDesign:
    def test_describe_and_parse_round_trip(self):
        space = RtlSpace({n: p.values for n, p in CATALOG.items()})
        design = space.design({"l2_mshr": 4, "home_alloc": "LOW", "scoreboard": 16})
        assert parse_design(design.describe(), space) == design

    def test_missing_parameters_take_their_defaults(self):
        space = RtlSpace({"l2_mshr": (4, 8), "dcache_wbuf": (4, 8)})
        assert parse_design("l2_mshr=4", space) == space.design({"l2_mshr": 4})

    @pytest.mark.parametrize(
        "text",
        ["l2_mshr", "l2_mshr=3", "l2_mshr=32", "nope=4", "l2_mshr=4 l2_mshr=8", "dcache_wbuf=4", "l2_mshr=four"],
    )
    def test_malformed_or_out_of_space_text_parses_to_none(self, text):
        space = RtlSpace({"l2_mshr": (4, 8, 16)})
        assert parse_design(text, space) is None

    def test_a_string_value_is_matched_without_regard_to_case(self):
        space = RtlSpace({"home_alloc": ("LOW", "MIXED")})
        assert parse_design("home_alloc=low", space) == space.design({"home_alloc": "LOW"})

    def test_the_json_names_every_value(self):
        design = RtlDesign.of({"l2_mshr": 4}, ("l2_mshr", "dcache_wbuf"))
        assert design.as_json() == '{"rtl": {"dcache_wbuf": 8, "l2_mshr": 4}}'

    def test_changed_lists_only_non_default_values(self):
        assert RtlDesign.of({"l2_mshr": 4, "dcache_wbuf": 8}).changed() == {"l2_mshr": 4}

    def test_an_unknown_parameter_is_refused(self):
        with pytest.raises(ValueError, match="unknown RTL parameters"):
            RtlDesign.of({"l3_mshr": 4})


class TestSpace:
    SPACE = RtlSpace({"l2_mshr": (4, 8, 16), "dcache_wbuf": (4, 8), "home_alloc": ("LOW", "MIXED")})

    def test_knobs_are_sorted_and_hold_the_allowed_values(self):
        assert self.SPACE.knobs == {"dcache_wbuf": (4, 8), "home_alloc": ("LOW", "MIXED"), "l2_mshr": (4, 8, 16)}

    def test_a_design_covers_exactly_the_searched_parameters(self):
        design = self.SPACE.design({"l2_mshr": 4, "scoreboard": 16})
        assert [n for n, _ in design.values] == ["dcache_wbuf", "home_alloc", "l2_mshr"]
        assert self.SPACE.contains(design)

    def test_a_design_with_another_parameter_set_is_outside_the_space(self):
        assert not self.SPACE.contains(RtlDesign.of({}))
        assert not self.SPACE.contains(RtlDesign((("dcache_wbuf", 8), ("home_alloc", "MIXED"), ("l2_mshr", 32))))

    def test_count_is_the_product_of_the_value_lists(self):
        assert self.SPACE.count() == 12

    def test_sampling_is_seeded(self):
        a = [self.SPACE.sample(random.Random(3)) for _ in range(5)]
        b = [self.SPACE.sample(random.Random(3)) for _ in range(5)]
        assert a == b
        assert all(self.SPACE.contains(d) for d in a)

    def test_one_at_a_time_starts_with_the_default_and_changes_one_value_each(self):
        designs = self.SPACE.one_at_a_time()
        assert designs[0] == self.SPACE.default()
        assert len(designs) == 1 + 1 + 1 + 2
        assert all(len(d.changed()) == 1 for d in designs[1:])
        assert len(set(designs)) == len(designs)

    def test_grid_varies_the_named_parameters_and_holds_the_rest(self):
        designs = self.SPACE.grid({"l2_mshr": (4, 16), "dcache_wbuf": (4, 8)})
        assert len(designs) == 4
        assert {d.as_dict()["home_alloc"] for d in designs} == {"MIXED"}

    def test_grid_refuses_a_parameter_outside_the_space(self):
        with pytest.raises(ValueError, match="outside the space"):
            self.SPACE.grid({"scoreboard": (4,)})

    @pytest.mark.parametrize(
        "values,match",
        [
            ({}, "at least one"),
            ({"l3_mshr": (4, 8)}, "unknown RTL parameter"),
            ({"l2_mshr": (4, 8, 32)}, "must come from"),
            ({"l2_mshr": (4, 16)}, "include the default"),
            ({"l2_mshr": ()}, "must come from"),
        ],
    )
    def test_a_malformed_space_is_refused(self, values, match):
        with pytest.raises(ValueError, match=match):
            RtlSpace(values)

    def test_the_description_names_each_parameter_with_its_default_and_cost(self):
        text = self.SPACE.describe()
        assert "- l2_mshr (default 8; one of 4, 8, 16; 154 bits per tile per unit of the value)" in text
        assert "- home_alloc (default MIXED; one of LOW, MIXED; no storage)" in text


class TestDocsPage:
    """docs/06_mace_evaluation/rtl_params.md lists the catalog; a change to one must reach the other."""

    PAGE = Path(__file__).resolve().parents[2] / "docs" / "06_mace_evaluation" / "rtl_params.md"

    def _rows(self):
        rows = {}
        for line in self.PAGE.read_text().splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.startswith("| `") and len(cells) == 5:
                rows[cells[0].strip("`")] = cells
        return rows

    def test_the_table_lists_every_parameter(self):
        assert set(self._rows()) == set(CATALOG)

    def test_each_row_matches_its_parameter(self):
        for name, (_, default, values, bits, _) in self._rows().items():
            param = CATALOG[name]
            assert default == str(param.default), name
            assert values == ", ".join(str(v) for v in param.values), name
            assert bits == str(param.bits_per_unit), name

