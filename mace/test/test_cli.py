"""Tier-0 tests for mace.cli -- session state, spec-file parsing, config
storage, and the shell's command handlers (called directly, not through
cmd.Cmd's own input loop -- see mace/cli/shell.py's module docstring on why
that split makes this testable at all).

Run:
    pytest mace/test/test_cli.py -q
"""

from __future__ import annotations

import pytest

from mace import metrics
from mace.cli.config import apply_env_to_environment, load_env_file, write_env_file
from mace.cli.session import KNOWN_MESH_OUTCOMES, Session, detect_core, mesh_for_core_count
from mace.cli.spec_file import parse_spec_file
from mace.cli.shell import (
    build_spec_from_session,
    find_coverage_dat,
    format_report,
    generate_coverage_report,
    handle_read_spec,
    handle_read_verilog,
    handle_set_core,
    handle_top_module,
    no_adapter_post_mortem,
    resolve_verilator_coverage,
)
from mace.spec import LoopResult, MaceSpec, PostMortem, StepResult, Task


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

    def test_target_mesh_raises_for_a_zero_core_count_not_silently_none(self):
        """0 is a set (falsy) value, not an unset one -- it must reach
        mesh_for_core_count's own validation, which rejects it, rather than
        being treated the same as "never called set_core" (see
        handle_set_core's own ValueError handling for why this matters).
        """
        s = Session(piton_root="/x", core_count=0)
        with pytest.raises(ValueError, match="positive int"):
            s.target_mesh

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

    def test_core_key_sets_top_module_and_is_detected(self, tmp_path):
        f = tmp_path / "spec.txt"
        f.write_text("objective: verify barrier_atomic passes\nworkloads: a.c, b.c\ncore: pico\n")
        session = Session(piton_root="/x")

        handle_read_spec(session, str(f))

        assert session.top_module == "pico"
        assert session.detected_core == "pico"


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

    def test_zero_is_a_clean_error_not_a_crash(self):
        session = Session(piton_root="/x")
        msg = handle_set_core(session, "0")
        assert msg.startswith("ERROR")
        assert session.core_count is None  # not left stuck at 0
        assert session.target_mesh is None


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

    def test_coverage_defaults_off(self):
        assert build_spec_from_session(Session(piton_root="/x")).coverage is False

    def test_coverage_flows_through_from_session(self):
        session = Session(piton_root="/x", coverage=True)
        assert build_spec_from_session(session).coverage is True


class TestNoAdapterPostMortem:
    def test_names_the_unmatched_top_module(self):
        session = Session(piton_root="/x", top_module="my_custom_core")
        pm = no_adapter_post_mortem(session)
        assert pm.assessment == "likely_hardware_limitation"
        assert "my_custom_core" in pm.explanation
        assert "ariane" in pm.explanation and "sparc" in pm.explanation and "pico" in pm.explanation


class TestDoRun:
    def test_no_adapter_path_clears_a_stale_coverage_report(self):
        """A coverage report left over from a PREVIOUS run must not survive
        into a run whose top_module has no adapter -- write_report would
        otherwise print an unrelated percentage next to status=no_adapter.
        See do_run's own comment on why the reset happens before either
        branch, not only after a real run_mace_loop call.
        """
        from mace.cli.shell import MaceShell

        session = Session(piton_root="/x", top_module="not_a_known_core")
        session.last_coverage = {"hit": 100, "total": 200, "percent": 50.0}
        shell = MaceShell(session, llm=None, db=None)

        shell.do_run("")

        assert session.last_coverage is None
        assert session.last_result.status == "no_adapter"


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

    def test_includes_coverage_when_present(self):
        session = Session(piton_root="/x", top_module="custom")
        session.last_result = LoopResult(run_id="r1", status="passed", iterations=())
        session.last_coverage = {"hit": 8749, "total": 24311, "percent": 35.0}
        text = format_report(session)
        assert "coverage: 35.00% (8749/24311)" in text

    def test_coverage_failure_is_reported_not_hidden(self):
        session = Session(piton_root="/x", top_module="custom")
        session.last_result = LoopResult(run_id="r1", status="passed", iterations=())
        session.last_coverage = {"hit": None, "total": None, "percent": None}
        text = format_report(session)
        assert "coverage: requested but report generation failed" in text

    def test_no_coverage_section_when_never_requested(self):
        session = Session(piton_root="/x", top_module="custom")
        session.last_result = LoopResult(run_id="r1", status="passed", iterations=())
        assert "coverage" not in format_report(session)

    def test_includes_per_module_status_when_db_has_unit_test_tasks(self, tmp_path):
        from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig

        db = metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        spec = MaceSpec(workloads=("hello_world.c",), objective="bring up pico")
        run_id = metrics.start_run(db, spec, run_id="r1")
        build = PitonBuildArtifact(
            success=True, returncode=0, config=PitonConfig(), sim_type="vlt",
            model_dir="/x", binary_path="/x/Vpicorv32_ut_top", wall_time_s=16.0,
        )
        task = Task(id="t1", deps=(), kind="unit_test", spec="picorv32.v")
        step = StepResult(task=task, query=None, build=build, run=None, passed=True)
        metrics.record_iteration(db, run_id, 0, (step,), wall_s=16.0)

        session = Session(piton_root="/x", top_module="custom")
        session.last_result = LoopResult(run_id="r1", status="passed", iterations=())

        text = format_report(session, db)

        assert "per-module status:" in text
        assert "picorv32: build OK (task t1, iteration 0)" in text


def _fake_run_result(run_dir: str, verdict: str = "pass"):
    from chia_openpiton.state_def import PitonRunResult

    return PitonRunResult(
        success=verdict == "pass", returncode=0, test="x.c", sim_type="vlt",
        run_dir=run_dir, verdict=verdict,
    )


class TestFindCoverageDat:
    def test_finds_a_real_coverage_dat_in_the_last_iteration(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "coverage.dat").write_text("data")
        step = StepResult(
            task=Task(id="t1", kind="workload", spec="x", deps=()),
            query=None, build=None, run=_fake_run_result(str(run_dir)), passed=True,
        )
        assert find_coverage_dat(((step,),)) == str(run_dir / "coverage.dat")

    def test_prefers_the_latest_iteration_with_a_dat_file(self, tmp_path):
        old_dir, new_dir = tmp_path / "old", tmp_path / "new"
        old_dir.mkdir(); new_dir.mkdir()
        (old_dir / "coverage.dat").write_text("stale")
        (new_dir / "coverage.dat").write_text("fresh")
        old_step = StepResult(
            task=Task(id="t1", kind="workload", spec="x", deps=()),
            query=None, build=None, run=_fake_run_result(str(old_dir)), passed=True,
        )
        new_step = StepResult(
            task=Task(id="t2", kind="workload", spec="x", deps=()),
            query=None, build=None, run=_fake_run_result(str(new_dir)), passed=True,
        )
        assert find_coverage_dat(((old_step,), (new_step,))) == str(new_dir / "coverage.dat")

    def test_none_when_no_run_ever_produced_one(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        step = StepResult(
            task=Task(id="t1", kind="workload", spec="x", deps=()),
            query=None, build=None, run=_fake_run_result(str(run_dir)), passed=True,
        )
        assert find_coverage_dat(((step,),)) is None

    def test_none_for_a_config_only_iteration(self):
        step = StepResult(
            task=Task(id="t1", kind="config", spec="x", deps=()),
            query=None, build=None, run=None, passed=True,
        )
        assert find_coverage_dat(((step,),)) is None

    def test_none_for_empty_iterations(self):
        assert find_coverage_dat(()) is None


class TestResolveVerilatorCoverage:
    """resolve_verilator_coverage() runs in the user's own interactive CLI
    process, never inside a Ray worker, so it never inherits
    cluster/local.yaml's worker_env_commands PATH/VERILATOR_ROOT fix -- it
    has to make its own call about what's actually safe to run."""

    def test_nothing_on_path_falls_back_to_stable_binary(self, monkeypatch):
        monkeypatch.setattr("mace.cli.shell.shutil.which", lambda name: None)
        assert resolve_verilator_coverage() == "/usr/bin/verilator_coverage"

    def test_path_binary_that_faults_on_version_falls_back(self, monkeypatch):
        """The exact real-world case this exists for: a broken devel-
        snapshot verilator_coverage that's first on PATH faults on
        --version alone (confirmed directly, see
        scripts/local_coverage_1x1_build_test.py)."""
        from types import SimpleNamespace

        monkeypatch.setattr(
            "mace.cli.shell.shutil.which", lambda name: "/usr/local/bin/verilator_coverage"
        )

        def fake_run(cmd, **kw):
            assert cmd == ["/usr/local/bin/verilator_coverage", "--version"]
            return SimpleNamespace(returncode=1, stdout="", stderr="internal fault, sorry")

        monkeypatch.setattr("mace.cli.shell.subprocess.run", fake_run)
        assert resolve_verilator_coverage() == "/usr/bin/verilator_coverage"

    def test_path_binary_that_crashes_outright_falls_back(self, monkeypatch):
        """--version can also fail by raising, not just returning nonzero --
        e.g. the binary segfaults instead of exiting cleanly."""
        monkeypatch.setattr(
            "mace.cli.shell.shutil.which", lambda name: "/usr/local/bin/verilator_coverage"
        )

        def fake_run(cmd, **kw):
            raise OSError("segfault")

        monkeypatch.setattr("mace.cli.shell.subprocess.run", fake_run)
        assert resolve_verilator_coverage() == "/usr/bin/verilator_coverage"

    def test_working_path_binary_is_preferred_over_the_stable_fallback(self, monkeypatch):
        """The whole point of this resolver: when PATH actually resolves to
        a healthy binary (e.g. a Nix devShell or a correctly-configured
        worker env), use it -- it's the one self-consistent with whatever
        built the model, not an unrelated fixed version."""
        from types import SimpleNamespace

        monkeypatch.setattr(
            "mace.cli.shell.shutil.which",
            lambda name: "/nix/store/xyz-verilator-5.052/bin/verilator_coverage",
        )

        def fake_run(cmd, **kw):
            assert cmd == ["/nix/store/xyz-verilator-5.052/bin/verilator_coverage", "--version"]
            return SimpleNamespace(returncode=0, stdout="Verilator 5.052\n", stderr="")

        monkeypatch.setattr("mace.cli.shell.subprocess.run", fake_run)
        assert (
            resolve_verilator_coverage()
            == "/nix/store/xyz-verilator-5.052/bin/verilator_coverage"
        )


class TestGenerateCoverageReport:
    def test_uses_whatever_resolve_verilator_coverage_picks(self, monkeypatch):
        """generate_coverage_report itself doesn't duplicate the PATH-vs-
        fallback decision -- it just defers to resolve_verilator_coverage()
        and runs --annotate against whatever that returns."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            from types import SimpleNamespace
            return SimpleNamespace(stdout="Total coverage (1/2) 50.00%\n", stderr="")

        monkeypatch.setattr(
            "mace.cli.shell.resolve_verilator_coverage", lambda: "/some/resolved/verilator_coverage"
        )
        monkeypatch.setattr("mace.cli.shell.subprocess.run", fake_run)
        summary, raw = generate_coverage_report("/some/run/coverage.dat")

        assert calls[0][0] == "/some/resolved/verilator_coverage"
        assert calls[0][1] == "--annotate"
        assert summary == {"hit": 1, "total": 2, "percent": 50.0}
        assert "50.00%" in raw

    def test_failure_surfaces_as_none_not_a_wrong_number(self, monkeypatch):
        def fake_run(cmd, **kw):
            from types import SimpleNamespace
            return SimpleNamespace(stdout="", stderr="%Error: Verilator_coverage internal fault, sorry.\n")

        monkeypatch.setattr(
            "mace.cli.shell.resolve_verilator_coverage", lambda: "/usr/bin/verilator_coverage"
        )
        monkeypatch.setattr("mace.cli.shell.subprocess.run", fake_run)
        summary, _ = generate_coverage_report("/some/run/coverage.dat")
        assert summary == {"hit": None, "total": None, "percent": None}
