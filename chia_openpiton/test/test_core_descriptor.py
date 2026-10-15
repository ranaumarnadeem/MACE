"""Tier-0 tests for chia_openpiton.core_descriptor: parsing, the support
check run before any build, and the Flist.core and link.ld generators.

Run:
    pytest chia_openpiton/test/test_core_descriptor.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chia_openpiton.core_descriptor import (
    FLIST_CORE,
    LINK_LD,
    SKELETON_MARK,
    TILE_DIR,
    apply_descriptor,
    boot_problems,
    flist_core,
    link_ld,
    load_descriptor,
    missing_files,
    parse_descriptor,
    support_problems,
    wrapper_skeleton,
)
from chia_openpiton.parse import first_requests

REPO = Path(__file__).resolve().parents[2]
PICORV32 = REPO / "examples" / "cores" / "picorv32.yaml"


def ibex(**override) -> dict:
    data = {
        "core": "ibex",
        "top_module": "ibex_top",
        "wrapper": "ibex/generic_core_wrapper.sv",
        "sources": ["ibex/rtl/ibex_pkg.sv", "ibex/rtl/ibex_top.sv"],
        "include_dirs": ["ibex/vendor/prim/rtl"],
        "isa": {"march": "rv32imc", "mabi": "ilp32"},
        "bus": "obi",
        "reset_vector": 0x4000_0080,
        "atomics": "none",
    }
    data.update(override)
    return data


class TestParse:
    def test_the_picorv32_example_is_valid_and_supported(self):
        desc = load_descriptor(str(PICORV32))
        assert (desc.core, desc.bus, desc.atomics, desc.march) == ("picorv32", "simple", "bus", "rv32ima")
        assert support_problems(desc) == ()

    def test_defaults(self):
        desc = parse_descriptor(ibex())
        assert desc.ram_base == 0x4000_0000
        assert (desc.private_cache, desc.harts, desc.own_interconnect, desc.hart_id) == ("none", 1, False, "register")
        assert desc.notes == ""

    @pytest.mark.parametrize("missing", ["core", "wrapper", "sources", "isa", "bus", "reset_vector", "atomics"])
    def test_a_missing_required_field_is_rejected(self, missing):
        data = ibex()
        del data[missing]
        with pytest.raises(ValueError, match=f"missing descriptor fields: {missing}"):
            parse_descriptor(data)

    def test_an_unknown_field_is_rejected(self):
        with pytest.raises(ValueError, match="unknown descriptor fields: cache"):
            parse_descriptor(ibex(cache="none"))

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("bus", "axi", "bus must be one of simple, obi, wishbone"),
            ("atomics", "yes", "atomics must be one of none, bus"),
            ("private_cache", "big", "private_cache must be one of"),
            ("hart_id", "csr", "hart_id must be one of register, port"),
            ("core", "Ibex", "core must be a lower-case name"),
            ("top_module", "ibex top", "top_module must be a Verilog module name"),
            ("isa", {"march": "x86", "mabi": "ilp32"}, "isa.march must look like"),
            ("isa", {"march": "rv32i"}, "isa must be a mapping with march and mabi"),
            ("reset_vector", "0x40000000", "reset_vector must be a non-negative int"),
            ("harts", 0, "harts must be at least 1"),
            ("sources", [], "sources must list at least one file"),
            ("sources", "ibex/rtl/ibex_top.sv", "sources must be a list"),
        ],
    )
    def test_malformed_values_are_rejected(self, field, value, message):
        with pytest.raises(ValueError, match=message):
            parse_descriptor(ibex(**{field: value}))

    @pytest.mark.parametrize("path", ["../ibex/top.sv", "/etc/passwd", "ibex/../../verif/x.v", "C:/x.v"])
    def test_paths_must_stay_under_the_tile_directory(self, path):
        with pytest.raises(ValueError, match="must stay under piton/design/chip/tile/"):
            parse_descriptor(ibex(wrapper=path))

    def test_the_digest_follows_the_content(self):
        a = parse_descriptor(ibex())
        assert a.digest == parse_descriptor(ibex()).digest
        assert a.digest != parse_descriptor(ibex(reset_vector=0x4000_0000)).digest


class TestSupportCheck:
    def test_an_ibex_like_core_is_supported(self):
        assert support_problems(parse_descriptor(ibex())) == ()

    def test_an_ariane_like_core_is_rejected_for_rv64_and_its_cache(self):
        problems = support_problems(parse_descriptor(ibex(
            isa={"march": "rv64imafdc", "mabi": "lp64d"}, private_cache="write_back")))
        assert len(problems) == 3
        assert "RV64" in problems[0]
        assert "write-back private cache" in problems[1]
        assert "A extension but atomics is none" in problems[2]

    def test_a_soc_with_its_own_interconnect_is_rejected(self):
        problems = support_problems(parse_descriptor(ibex(own_interconnect=True)))
        assert problems == ("the core brings its own L2, L3, or NoC; the generic tile connects a core to "
                            "the tile's L1.5 only",)

    def test_a_disabled_cache_is_supported_and_a_write_through_one_is_not(self):
        assert support_problems(parse_descriptor(ibex(private_cache="disabled"))) == ()
        assert "write-through" in support_problems(parse_descriptor(ibex(private_cache="write_through")))[0]

    def test_ram_at_or_above_bit_31_is_rejected(self):
        problems = support_problems(parse_descriptor(ibex(ram_base=0x8000_0000, reset_vector=0x8000_0000)))
        assert any("ram_base is 0x80000000" in p for p in problems)
        assert any("reset_vector 0x80000000" in p for p in problems)

    @pytest.mark.parametrize("vector", [0x3FFF_FFFC, 0x4000_1000, 0x4000_0082])
    def test_a_reset_vector_outside_the_first_page_or_unaligned_is_rejected(self, vector):
        (problem,) = support_problems(parse_descriptor(ibex(reset_vector=vector)))
        assert problem.startswith(f"reset_vector {vector:#x}")

    def test_atomics_need_the_simple_bus(self):
        (problem,) = support_problems(parse_descriptor(ibex(atomics="bus", isa={"march": "rv32imac", "mabi": "ilp32"})))
        assert "obi bridge does not" in problem

    def test_more_than_one_hart_is_rejected(self):
        (problem,) = support_problems(parse_descriptor(ibex(harts=2)))
        assert "2 harts" in problem

    def test_g_counts_as_having_atomics(self):
        (problem,) = support_problems(parse_descriptor(ibex(isa={"march": "rv32g", "mabi": "ilp32d"})))
        assert "A extension" in problem


class TestGenerators:
    def test_flist_core_lists_include_dirs_sources_then_the_wrapper(self):
        lines = flist_core(parse_descriptor(ibex())).splitlines()
        assert lines[0].startswith("// ibex")
        assert lines[1:] == [
            "+incdir+../../ibex/vendor/prim/rtl",
            "../../ibex/rtl/ibex_pkg.sv",
            "../../ibex/rtl/ibex_top.sv",
            "../../ibex/generic_core_wrapper.sv",
        ]

    def test_fix_14_s_default_flist_core_is_the_picorv32_descriptor_s(self):
        default = (REPO / "scripts/openpiton_generic" / FLIST_CORE).read_text()
        assert flist_core(load_descriptor(str(PICORV32))) == default

    def test_link_ld_places_text_init_at_the_reset_vector(self):
        text = link_ld(parse_descriptor(ibex()))
        assert "  . = 0x40000000;\n" in text
        assert "  .text.init : { . = 0x80; *(.text.init) }\n" in text

    def test_link_ld_without_an_offset_matches_the_default_layout(self):
        text = link_ld(load_descriptor(str(PICORV32)))
        assert "  .text.init : { *(.text.init) }\n" in text

    def test_sims_flags_carry_the_isa(self):
        assert parse_descriptor(ibex()).sims_flags() == ("-rv32_march=rv32imc", "-rv32_mabi=ilp32")

    def test_ladder_tests_follow_the_isa_and_atomics(self):
        steps = parse_descriptor(ibex()).ladder_tests()
        assert "mul.S" in steps["isa"] and "atomics" not in steps
        assert "sw.S" in steps["isa"] and "sw.S" not in steps["mesh"] and "mul.S" in steps["mesh"]
        steps = parse_descriptor(ibex(isa={"march": "rv32i", "mabi": "ilp32"})).ladder_tests()
        assert "mul.S" not in steps["isa"]
        assert load_descriptor(str(PICORV32)).ladder_tests()["atomics"] == ("generic_amo_counter.S",)


class TestBootCheck:
    LOG = (
        "250: good_trap 0040000318\n"
        "1250: generic tile 1: first request at 40000080\n"
        "1250: generic tile 0: first request at 40000080\n"
        "9000: generic tile 0: first request at 40000000\n"
    )

    def test_first_requests_keeps_each_tile_s_first_line(self):
        assert first_requests(self.LOG) == {0: 0x4000_0080, 1: 0x4000_0080}

    def test_every_tile_at_the_reset_vector_passes(self):
        assert boot_problems(parse_descriptor(ibex()), first_requests(self.LOG), 2) == ()

    def test_a_wrong_reset_vector_is_reported_per_tile(self):
        problems = boot_problems(parse_descriptor(ibex(reset_vector=0x4000_0000)), first_requests(self.LOG), 2)
        assert problems == tuple(
            f"tile {t}: the core's first request was at 0x40000080, not the descriptor's reset_vector 0x40000000"
            for t in (0, 1)
        )

    def test_a_silent_tile_is_reported(self):
        (problem,) = boot_problems(parse_descriptor(ibex()), first_requests(self.LOG), 3)
        assert problem.startswith("tile 2: the core made no memory request after reset")


@pytest.fixture
def patched_root(tmp_path) -> Path:
    """A checkout with the directories fixes 14-17 create, and Ibex's files."""
    for rel in (FLIST_CORE, LINK_LD):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    tile = tmp_path / TILE_DIR
    for rel in ("ibex/rtl/ibex_pkg.sv", "ibex/rtl/ibex_top.sv", "ibex/generic_core_wrapper.sv"):
        (tile / rel).parent.mkdir(parents=True, exist_ok=True)
        (tile / rel).write_text("// rtl\n")
    (tile / "ibex/vendor/prim/rtl").mkdir(parents=True)
    return tmp_path


class TestApply:
    def test_it_writes_flist_core_and_link_ld(self, patched_root):
        desc = parse_descriptor(ibex())
        assert apply_descriptor(desc, str(patched_root)) == (FLIST_CORE, LINK_LD)
        assert (patched_root / FLIST_CORE).read_text() == flist_core(desc)
        assert (patched_root / LINK_LD).read_text() == link_ld(desc)

    def test_an_unsupported_core_writes_nothing(self, patched_root):
        with pytest.raises(ValueError, match="unsupported core: .*RV64"):
            apply_descriptor(parse_descriptor(ibex(isa={"march": "rv64imc", "mabi": "lp64"})), str(patched_root))
        assert not (patched_root / FLIST_CORE).exists()

    def test_missing_files_are_named_and_nothing_is_written(self, patched_root):
        os.remove(patched_root / TILE_DIR / "ibex/rtl/ibex_top.sv")
        desc = parse_descriptor(ibex())
        assert missing_files(desc, str(patched_root)) == ("ibex/rtl/ibex_top.sv",)
        with pytest.raises(ValueError, match="ibex/rtl/ibex_top.sv"):
            apply_descriptor(desc, str(patched_root))
        assert not (patched_root / FLIST_CORE).exists()

    def test_an_unpatched_checkout_is_refused(self, patched_root):
        (patched_root / LINK_LD).parent.rmdir()
        with pytest.raises(ValueError, match="patch the checkout with fixes 14-17 first"):
            apply_descriptor(parse_descriptor(ibex()), str(patched_root))
        assert not (patched_root / FLIST_CORE).exists()


class TestCommandLine:
    def test_check_prints_supported_and_exits_0(self, capsys):
        from chia_openpiton.core_descriptor import main

        assert main(["check", str(PICORV32)]) == 0
        assert capsys.readouterr().out == "picorv32: supported\n"

    def test_check_prints_each_problem_and_exits_1(self, tmp_path, capsys):
        import yaml

        from chia_openpiton.core_descriptor import main

        path = tmp_path / "soc.yaml"
        path.write_text(yaml.safe_dump(ibex(own_interconnect=True, harts=2)))
        assert main(["check", str(path)]) == 1
        out = capsys.readouterr().out.splitlines()
        assert len(out) == 2 and all(line.startswith("unsupported: ") for line in out)

    def test_apply_writes_the_files(self, patched_root, tmp_path, capsys):
        import yaml

        from chia_openpiton.core_descriptor import main

        path = tmp_path / "ibex.yaml"
        path.write_text(yaml.safe_dump(ibex()))
        assert main(["apply", str(path), str(patched_root)]) == 0
        assert (patched_root / FLIST_CORE).exists()


class TestWrapperSkeleton:
    def test_a_missing_wrapper_gets_a_skeleton(self, patched_root):
        os.remove(patched_root / TILE_DIR / "ibex/generic_core_wrapper.sv")
        desc = parse_descriptor(ibex())
        assert missing_files(desc, str(patched_root)) == ("ibex/generic_core_wrapper.sv",)
        assert missing_files(desc, str(patched_root), wrapper=False) == ()
        written = apply_descriptor(desc, str(patched_root))
        assert written == (FLIST_CORE, LINK_LD, "piton/design/chip/tile/ibex/generic_core_wrapper.sv")
        text = (patched_root / TILE_DIR / "ibex/generic_core_wrapper.sv").read_text()
        assert text == wrapper_skeleton(desc)

    def test_an_existing_wrapper_is_kept(self, patched_root):
        apply_descriptor(parse_descriptor(ibex()), str(patched_root))
        assert (patched_root / TILE_DIR / "ibex/generic_core_wrapper.sv").read_text() == "// rtl\n"

    def test_the_skeleton_has_the_fixed_ports_and_drives_no_request(self):
        text = wrapper_skeleton(parse_descriptor(ibex()))
        assert "module generic_core_wrapper (" in text
        for port in ("clk", "rst_n", "hart_id", "bus_valid", "bus_addr", "bus_wstrb", "bus_wdata",
                     "bus_amo_op", "bus_ready", "bus_rdata", "wake"):
            assert f" {port}" in text, port
        assert "assign bus_valid  = 1'b0;" in text
        assert "generic_bridge_obi" in text and "0x40000080" in text
        assert text.count(SKELETON_MARK) == 2
