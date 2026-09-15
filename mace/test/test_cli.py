"""Tier-0 tests for mace.cli -- session state, spec-file parsing, config
storage, and the shell's command handlers (called directly, not through
cmd.Cmd's own input loop -- see mace/cli/shell.py's module docstring on why
that split makes this testable at all).

Run:
    pytest mace/test/test_cli.py -q
"""

from __future__ import annotations

import pytest

from mace.cli.config import apply_env_to_environment, load_env_file, write_env_file
from mace.cli.session import KNOWN_MESH_OUTCOMES, Session, detect_core, mesh_for_core_count
from mace.cli.spec_file import parse_spec_file
from mace.cli.shell import (
    build_spec_from_session,
    format_report,
    handle_read_spec,
    handle_read_verilog,
    handle_set_core,
    handle_top_module,
    no_adapter_post_mortem,
)
from mace.spec import LoopResult, PostMortem


class TestDetectCore:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("ariane_top", "ariane"),
            ("cva6_wrapper", "ariane"),
            ("my_sparc_core", "sparc"),
            ("OpenSPARC_T1", "sparc"),
            ("picorv32_top", "pico"),
            ("PICO_wrapper", "pico"),
        ],
    )
    def test_matches_by_case_insensitive_substring(self, name, expected):
        assert detect_core(name) == expected

    def test_unknown_core_returns_none(self):
        assert detect_core("my_custom_risc_core") is None


class TestMeshForCoreCount:
    @pytest.mark.parametrize("n,expected", [(1, (1, 1)), (4, (2, 2)), (16, (4, 4)), (9, (3, 3))])
    def test_perfect_squares_are_square_meshes(self, n, expected):
        assert mesh_for_core_count(n) == expected

    def test_non_square_prefers_narrowest_rectangle(self):
        assert mesh_for_core_count(6) == (3, 2)

    def test_prime_falls_back_to_a_row(self):
        assert mesh_for_core_count(7) == (7, 1)

    @pytest.mark.parametrize("bad", [0, -1, 1.5, True])
    def test_non_positive_int_raises(self, bad):
        with pytest.raises(ValueError, match="positive int"):
            mesh_for_core_count(bad)

    def test_every_known_mesh_outcome_key_actually_resolves(self):
        for n in KNOWN_MESH_OUTCOMES:
            mesh_for_core_count(n)  # must not raise


class TestSession:
    def test_detected_core_is_none_before_top_module_set(self):
        s = Session(piton_root="/x")
        assert s.detected_core is None

    def test_target_mesh_is_none_before_core_count_set(self):
        s = Session(piton_root="/x")
        assert s.target_mesh is None

    def test_detected_core_updates_after_top_module(self):
        s = Session(piton_root="/x")
        s.top_module = "ariane_core"
        assert s.detected_core == "ariane"


class TestParseSpecFile:
    def test_whole_file_becomes_objective_when_no_keys_found(self):
        overrides = parse_spec_file("Bring up a 2x2 mesh and verify coherence holds.\n")
        assert overrides == {"objective": "Bring up a 2x2 mesh and verify coherence holds."}

    def test_structured_keys_are_extracted(self):
        text = "objective: verify barrier_atomic passes\nworkloads: a.c, b.c\ncore: pico\n"
        overrides = parse_spec_file(text)
        assert overrides == {
            "objective": "verify barrier_atomic passes",
            "workloads": ("a.c", "b.c"),
            "core": "pico",
        }

    def test_keys_are_case_insensitive(self):
        assert parse_spec_file("OBJECTIVE: x\n") == {"objective": "x"}

    def test_empty_file_yields_no_objective(self):
        assert parse_spec_file("   \n") == {}

    def test_unrecognized_lines_are_ignored_not_raised(self):
        text = "objective: x\nsome_random_line: y\n"
        assert parse_spec_file(text) == {"objective": "x"}


class TestConfig:
    def test_write_and_load_round_trip(self, tmp_path):
        path = write_env_file("opencode", "sk-test-123", tmp_path / ".env")
        assert load_env_file(path) == {"OPENCODE_API_KEY": "sk-test-123"}

    def test_load_raises_when_file_does_not_exist(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_env_file(tmp_path / "nope.env")

    def test_load_skips_blank_and_comment_lines(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("# a comment\n\nOPENCODE_API_KEY=sk-x\n")
        assert load_env_file(f) == {"OPENCODE_API_KEY": "sk-x"}

    def test_load_strips_matching_quotes(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text('OPENCODE_API_KEY="sk-x"\n')
        assert load_env_file(f) == {"OPENCODE_API_KEY": "sk-x"}

    def test_write_falls_back_to_generic_var_for_an_unknown_backend(self, tmp_path):
        path = write_env_file("some_future_backend", "sk-x", tmp_path / ".env")
        assert load_env_file(path) == {"MACE_LLM_API_KEY": "sk-x"}

    def test_apply_sets_environment_variables(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        apply_env_to_environment({"ANTHROPIC_API_KEY": "sk-x"})
        import os

        assert os.environ["ANTHROPIC_API_KEY"] == "sk-x"


class TestHandleReadVerilog:
    def test_registers_existing_files(self, tmp_path):
        f = tmp_path / "core.v"
        f.write_text("module core; endmodule\n")
        session = Session(piton_root="/x")

        msg = handle_read_verilog(session, str(f))

        assert session.verilog_files == (f,)
        assert "core.v" in msg

    def test_missing_file_is_an_error_and_does_not_update_session(self, tmp_path):
        session = Session(piton_root="/x")
        msg = handle_read_verilog(session, str(tmp_path / "nope.v"))
        assert msg.startswith("ERROR")
        assert session.verilog_files == ()

    def test_no_argument_is_an_error(self):
        session = Session(piton_root="/x")
        assert handle_read_verilog(session, "").startswith("ERROR")


class TestHandleTopModule:
    def test_known_core_name_reports_the_match(self):
        session = Session(piton_root="/x")
        msg = handle_top_module(session, "ariane_wrapper")
        assert session.top_module == "ariane_wrapper"
        assert "supported core" in msg

    def test_unknown_core_name_reports_no_adapter(self):
        session = Session(piton_root="/x")
        msg = handle_top_module(session, "my_custom_core")
        assert "does not match any core" in msg


class TestHandleReadSpec:
    def test_reads_objective_from_file(self, tmp_path):
        f = tmp_path / "spec.txt"
        f.write_text("verify scatter_gather on a 1x1 mesh\n")
        session = Session(piton_root="/x")

        handle_read_spec(session, str(f))

        assert session.objective == "verify scatter_gather on a 1x1 mesh"

    def test_missing_file_is_an_error(self, tmp_path):
        session = Session(piton_root="/x")
        msg = handle_read_spec(session, str(tmp_path / "nope.txt"))
        assert msg.startswith("ERROR")


class TestHandleSetCore:
    def test_sets_a_known_mesh_and_reports_its_outcome(self):
        session = Session(piton_root="/x")
        msg = handle_set_core(session, "4")
        assert session.target_mesh == (2, 2)
        assert "hangs" in msg  # KNOWN_MESH_OUTCOMES[4]'s real, honest finding

    def test_sets_an_unvalidated_count_and_says_so(self):
        session = Session(piton_root="/x")
        msg = handle_set_core(session, "6")
        assert session.target_mesh == (3, 2)
        assert "unvalidated" in msg

    def test_non_integer_is_an_error(self):
        session = Session(piton_root="/x")
        assert handle_set_core(session, "four").startswith("ERROR")


class TestBuildSpecFromSession:
    def test_defaults_when_nothing_set(self):
        spec = build_spec_from_session(Session(piton_root="/x"))
        assert spec.core == "ariane"
        assert spec.workloads == ("barrier_atomic.c",)
        assert spec.target_mesh == (1, 1)

    def test_uses_accumulated_state(self):
        session = Session(
            piton_root="/x", top_module="picorv32_top", objective="verify it",
            workloads=("scatter_gather.c",), core_count=1,
        )
        spec = build_spec_from_session(session)
        assert (spec.core, spec.objective, spec.workloads) == ("pico", "verify it", ("scatter_gather.c",))


class TestNoAdapterPostMortem:
    def test_names_the_unmatched_top_module(self):
        session = Session(piton_root="/x", top_module="my_custom_core")
        pm = no_adapter_post_mortem(session)
        assert pm.assessment == "likely_hardware_limitation"
        assert "my_custom_core" in pm.explanation
        assert "ariane" in pm.explanation and "sparc" in pm.explanation and "pico" in pm.explanation


class TestFormatReport:
    def test_no_run_yet(self):
        text = format_report(Session(piton_root="/x"))
        assert "no run has happened yet" in text

    def test_includes_post_mortem_when_present(self):
        session = Session(piton_root="/x", top_module="custom")
        session.last_result = LoopResult(
            run_id="r1", status="budget_exceeded", iterations=(),
            post_mortem=PostMortem(assessment="inconclusive", explanation="x", next_steps="y"),
        )
        text = format_report(session)
        assert "assessment: inconclusive" in text
        assert "explanation: x" in text
        assert "next_steps: y" in text
