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
    app,
    build_spec_from_session,
    find_coverage_dat,
    format_report,
    generate_coverage_report,
    handle_read_spec,
    handle_read_verilog,
    handle_set_core,
    handle_top_module,
    no_adapter_post_mortem,
    parse_script_lines,
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

    def test_a_count_too_large_for_any_mesh_raises_immediately(self):
        """256*256 = 65536 is the largest tile count any mesh could hold
        (MAX_TILES_PER_AXIS=256 per side) -- a huge n (e.g. an accidental
        extra digit typed into `set_core`) must be rejected up front, not
        walked through an O(sqrt(n)) trial-division loop first. Bounded by
        an explicit timeout: this is exactly the hang this check exists to
        prevent, so a regression here should make the test itself hang
        rather than merely fail an assertion.
        """
        import time

        started = time.monotonic()
        with pytest.raises(ValueError, match="cannot fit"):
            mesh_for_core_count(10**9)
        assert time.monotonic() - started < 1.0

    def test_a_count_that_only_factors_into_an_oversized_axis_raises(self):
        """65535 = 255 * 257 -- under the 65536-tile total cap, but its
        narrowest-rectangle factorization puts 257 tiles on one axis,
        over the 256-tile-per-axis limit. The total-count guard alone
        would miss this; the result's own axes must be checked too.
        """
        with pytest.raises(ValueError, match="exceeds the 256-tile-per-axis limit"):
            mesh_for_core_count(65535)


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

    def test_structured_keys_with_no_objective_line_do_not_pollute_it(self):
        """A file with only workloads:/core: lines is a genuine structured
        spec, not the plain-English case the whole-file fallback exists
        for -- it must not synthesize a garbled objective from the raw
        `workloads: ...\\ncore: ...` text.
        """
        overrides = parse_spec_file("workloads: foo.c\ncore: ariane\n")
        assert overrides == {"workloads": ("foo.c",), "core": "ariane"}

    def test_a_key_line_with_no_value_does_not_swallow_the_next_line(self):
        """objective:'s own \\s* after the colon used to match a trailing
        newline too, so an empty-valued key line bled the FOLLOWING line's
        text into its own value -- here, `core: ariane` would have become
        the "objective" instead of being parsed as its own key.
        """
        overrides = parse_spec_file("objective:\ncore: ariane\n")
        assert overrides == {"core": "ariane"}


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

    def test_write_for_a_second_backend_keeps_the_first_backends_key(self, tmp_path):
        path = write_env_file("opencode", "sk-opencode", tmp_path / ".env")
        write_env_file("claude", "sk-claude", path)
        assert load_env_file(path) == {
            "OPENCODE_API_KEY": "sk-opencode",
            "ANTHROPIC_API_KEY": "sk-claude",
        }

    def test_apply_sets_environment_variables(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        apply_env_to_environment({"ANTHROPIC_API_KEY": "sk-x"})
        import os

        assert os.environ["ANTHROPIC_API_KEY"] == "sk-x"


class TestInit:
    """backend=vertex authenticates via Google ADC, not a stored key -- it
    must not prompt for/write one the way every other backend does. See
    the `init` command's own comment for the live-user rough edge this
    guards against.
    """

    def test_bare_init_defaults_to_vertex_matching_shell(self, tmp_path, monkeypatch):
        """A first-time user running `mace init` then `mace shell` with no
        --backend on either must land on the same (funded) backend both
        times, not opencode on one and vertex on the other.
        """
        from typer.testing import CliRunner

        monkeypatch.setattr("mace.cli.shell.DEFAULT_ENV_PATH", tmp_path / ".env")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = CliRunner().invoke(app, ["init"])

        assert result.exit_code == 0
        assert "Google ADC" in result.output

    def test_vertex_writes_no_env_file_and_points_at_adc(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        monkeypatch.setattr("mace.cli.shell.DEFAULT_ENV_PATH", tmp_path / ".env")
        # Deterministic regardless of whether this machine happens to have
        # real gcloud ADC configured -- tmp_path is guaranteed not to.
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = CliRunner().invoke(app, ["init", "--backend", "vertex"])

        assert result.exit_code == 0
        assert not (tmp_path / ".env").exists()
        assert "Google ADC" in result.output
        assert "gcloud auth application-default login" in result.output
        assert "--backend vertex" in result.output
        assert "--api" not in result.output  # vertex's own run command omits it

    def test_windows_checks_appdata_not_dot_config(self, tmp_path, monkeypatch):
        """gcloud itself only writes ADC under ~/.config on Linux/macOS --
        on Windows it writes under %APPDATA%\\gcloud instead, so checking
        the POSIX path there always reported MISSING even right after a
        successful `gcloud auth application-default login`.
        """
        from typer.testing import CliRunner

        appdata = tmp_path / "AppData" / "Roaming"
        adc_dir = appdata / "gcloud"
        adc_dir.mkdir(parents=True)
        (adc_dir / "application_default_credentials.json").write_text("{}")
        monkeypatch.setattr("mace.cli.shell._is_windows", lambda: True)
        monkeypatch.setenv("APPDATA", str(appdata))

        result = CliRunner().invoke(app, ["init", "--backend", "vertex"])

        assert result.exit_code == 0
        assert "[OK] Application Default Credentials found" in result.output

    def test_non_vertex_backend_still_writes_the_env_file(self, tmp_path):
        from typer.testing import CliRunner

        env_file = tmp_path / ".env"
        result = CliRunner().invoke(
            app, ["init", "--backend", "opencode", "--api-key", "sk-test", "--env-file", str(env_file)]
        )

        assert result.exit_code == 0
        assert env_file.exists()
        assert load_env_file(env_file) == {"OPENCODE_API_KEY": "sk-test"}
        assert f"--api {env_file}" in result.output


class TestResults:
    """`mace results` -- a read-only report over the metrics db, no live
    session needed (see mace.metrics.all_runs/failure_taxonomy)."""

    def test_no_runs_recorded_says_so(self, tmp_path):
        from typer.testing import CliRunner

        db_path = tmp_path / "empty.db"
        metrics.open_db(str(db_path), ray_placement=False)

        result = CliRunner().invoke(app, ["results", "--db-path", str(db_path)])

        assert result.exit_code == 0
        assert "No runs recorded" in result.output

    def test_a_missing_database_is_an_error_and_is_not_created(self, tmp_path):
        from typer.testing import CliRunner

        db_path = tmp_path / "mistyped.db"

        result = CliRunner().invoke(app, ["results", "--db-path", str(db_path)])

        assert result.exit_code == 1
        assert "no metrics database" in result.output
        assert not db_path.exists()

    def test_lists_recorded_runs_with_their_summary_metrics(self, tmp_path):
        from typer.testing import CliRunner

        db_path = tmp_path / "runs.db"
        db = metrics.open_db(str(db_path), ray_placement=False)
        run_id = metrics.start_run(db, MaceSpec(workloads=("hello_world.c",), objective="bring up 1x1"))
        metrics.finish_run(db, run_id, "passed")

        result = CliRunner().invoke(app, ["results", "--db-path", str(db_path)])

        assert result.exit_code == 0
        assert run_id in result.output
        assert "passed" in result.output

    def test_run_id_shows_failure_taxonomy_instead_of_the_runs_table(self, tmp_path):
        from typer.testing import CliRunner

        db_path = tmp_path / "runs.db"
        db = metrics.open_db(str(db_path), ray_placement=False)
        run_id = metrics.start_run(db, MaceSpec(workloads=("hello_world.c",), objective="bring up 1x1"))
        metrics.record_failure(db, run_id, 0, "a", "timeout", recovered=True)

        result = CliRunner().invoke(app, ["results", "--db-path", str(db_path), "--run-id", run_id])

        assert result.exit_code == 0
        assert "timeout" in result.output

    def test_run_id_with_no_recorded_failures_says_so(self, tmp_path):
        from typer.testing import CliRunner

        db_path = tmp_path / "runs.db"
        db = metrics.open_db(str(db_path), ray_placement=False)
        run_id = metrics.start_run(db, MaceSpec(workloads=("hello_world.c",), objective="bring up 1x1"))

        result = CliRunner().invoke(app, ["results", "--db-path", str(db_path), "--run-id", run_id])

        assert result.exit_code == 0
        assert "No recorded failures" in result.output

    def test_trace_shows_the_plan_dispatch_triage_story(self, tmp_path):
        from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
        from typer.testing import CliRunner

        db_path = tmp_path / "runs.db"
        db = metrics.open_db(str(db_path), ray_placement=False)
        spec = MaceSpec(workloads=("hello_world.c",), objective="bring up 1x1")
        run_id = metrics.start_run(db, spec, run_id="r1")

        def step(task_id, passed, kind="config", verdict=None):
            build = PitonBuildArtifact(
                success=passed, returncode=0, config=PitonConfig(), sim_type="vlt",
                model_dir="/x", binary_path="/x/V", wall_time_s=1.0,
            )
            run = None
            if verdict is not None:
                run = PitonRunResult(
                    success=passed, returncode=0, test="x.c", sim_type="vlt",
                    run_dir="/x", verdict=verdict,
                )
            task = Task(id=task_id, deps=(), kind=kind, spec="x.v")
            return StepResult(task=task, query=None, build=build, run=run, passed=passed)

        metrics.record_iteration(
            db, run_id, 0,
            (step("t1", False), step("t2", True, kind="workload", verdict="pass")),
            wall_s=10.0, usd=0.1,
        )
        metrics.record_failure(db, run_id, 0, "t1", "config_error", fix="raise l1d size", recovered=True)
        metrics.record_iteration(db, run_id, 1, (step("t3", True),), wall_s=5.0, usd=0.05)
        metrics.finish_run(db, run_id, "passed")

        result = CliRunner().invoke(
            app, ["results", "--db-path", str(db_path), "--run-id", run_id, "--trace"]
        )

        assert result.exit_code == 0
        assert "Iteration 0" in result.output
        assert "Iteration 1" in result.output
        assert "t1 (config)" in result.output
        assert "TRIAGE" in result.output
        assert "config_error" in result.output
        assert "t3" in result.output

    def test_trace_with_unknown_run_id_says_so(self, tmp_path):
        from typer.testing import CliRunner

        db_path = tmp_path / "empty.db"
        metrics.open_db(str(db_path), ray_placement=False)

        result = CliRunner().invoke(
            app, ["results", "--db-path", str(db_path), "--run-id", "no-such-run", "--trace"]
        )

        assert result.exit_code == 0
        assert "No recorded run" in result.output

    def test_trace_without_run_id_is_a_clean_error(self, tmp_path):
        """--trace with no --run-id used to silently fall through to the
        cross-run table instead of erroring -- a typo/forgotten --run-id
        got a plausible-looking but entirely different report with no
        signal anything was wrong.
        """
        from typer.testing import CliRunner

        db_path = tmp_path / "empty.db"
        metrics.open_db(str(db_path), ray_placement=False)

        result = CliRunner().invoke(app, ["results", "--db-path", str(db_path), "--trace"])

        assert result.exit_code == 1
        assert "--trace needs --run-id" in result.output

    def test_trace_escapes_bracket_shaped_diagnosis_text(self, tmp_path):
        """diagnosis/fix/objective are real LLM free text (see
        mace.metrics.failure_taxonomy's own docstring) -- unescaped, Rich's
        markup parser silently deletes anything shaped like "[...]" instead
        of printing it literally, or raises MarkupError outright.
        """
        from typer.testing import CliRunner

        db_path = tmp_path / "runs.db"
        db = metrics.open_db(str(db_path), ray_placement=False)
        spec = MaceSpec(workloads=("hello_world.c",), objective="bring up 1x1")
        run_id = metrics.start_run(db, spec, run_id="r1")
        metrics.record_iteration(db, run_id, 0, (), wall_s=0.0)
        metrics.record_failure(
            db, run_id, 0, "t1", "wrong value (see [l1d_size])",
            fix="set [l1_size] correctly",
        )

        result = CliRunner().invoke(
            app, ["results", "--db-path", str(db_path), "--run-id", run_id, "--trace"]
        )

        assert result.exit_code == 0
        assert "[l1d_size]" in result.output
        assert "[l1_size]" in result.output

    def test_failure_taxonomy_table_escapes_bracket_shaped_diagnosis_text(self, tmp_path):
        from typer.testing import CliRunner

        db_path = tmp_path / "runs.db"
        db = metrics.open_db(str(db_path), ray_placement=False)
        spec = MaceSpec(workloads=("hello_world.c",), objective="bring up 1x1")
        run_id = metrics.start_run(db, spec, run_id="r1")
        metrics.record_failure(db, run_id, 0, "t1", "l1d cache size mismatch [expected 32KB]")

        result = CliRunner().invoke(app, ["results", "--db-path", str(db_path), "--run-id", run_id])

        assert result.exit_code == 0
        assert "[expected 32KB]" in result.output


class TestClusterCommands:
    """mace cluster up/down/status -- thin subprocess wrappers over chia
    up/chia down/ray status. Every test fakes subprocess.run so no real
    chia/ray process -- and no real, billed GCP compute -- is ever touched
    by running this test suite."""

    def _fake_run(self, monkeypatch, returncode=0):
        from types import SimpleNamespace

        calls = []

        def fake(cmd):
            calls.append(cmd)
            return SimpleNamespace(returncode=returncode)

        monkeypatch.setattr("mace.cli.shell.subprocess.run", fake)
        return calls

    def test_up_shells_out_to_chia_up_with_the_config_file(self, monkeypatch):
        from typer.testing import CliRunner

        calls = self._fake_run(monkeypatch)
        result = CliRunner().invoke(app, ["cluster", "up", "cluster/local.yaml"])

        assert result.exit_code == 0
        assert calls == [["chia", "up", "cluster/local.yaml"]]

    def test_up_forwards_yes_and_dry_run(self, monkeypatch):
        from typer.testing import CliRunner

        calls = self._fake_run(monkeypatch)
        result = CliRunner().invoke(
            app, ["cluster", "up", "cluster/local.yaml", "--yes", "--dry-run"]
        )

        assert result.exit_code == 0
        assert calls == [["chia", "up", "cluster/local.yaml", "--yes", "--dry-run"]]

    def test_down_shells_out_to_chia_down(self, monkeypatch):
        from typer.testing import CliRunner

        calls = self._fake_run(monkeypatch)
        result = CliRunner().invoke(app, ["cluster", "down", "cluster/local.yaml", "-y"])

        assert result.exit_code == 0
        assert calls == [["chia", "down", "cluster/local.yaml", "--yes"]]

    def test_status_proxies_to_ray_status(self, monkeypatch):
        from typer.testing import CliRunner

        calls = self._fake_run(monkeypatch)
        result = CliRunner().invoke(app, ["cluster", "status"])

        assert result.exit_code == 0
        assert calls == [["ray", "status"]]

    def test_nonzero_child_exit_code_propagates(self, monkeypatch):
        from typer.testing import CliRunner

        self._fake_run(monkeypatch, returncode=1)
        result = CliRunner().invoke(app, ["cluster", "status"])

        assert result.exit_code == 1

    def test_missing_binary_is_a_clean_error_not_a_raw_traceback(self, monkeypatch):
        """ray (or chia) missing from PATH entirely -- e.g. an unactivated
        venv -- used to crash with a raw FileNotFoundError traceback
        instead of chia's own ray_passthrough.py convention (friendly
        message, exit 127) for the identical situation.
        """
        from typer.testing import CliRunner

        def fake(cmd):
            raise FileNotFoundError(2, "No such file or directory", cmd[0])

        monkeypatch.setattr("mace.cli.shell.subprocess.run", fake)
        result = CliRunner().invoke(app, ["cluster", "status"])

        assert result.exit_code == 127
        assert "'ray' was not found on PATH" in result.output


class TestParseScriptLines:
    def test_strips_blank_lines_and_comments(self):
        text = "read_verilog a.v\n\n# a comment\n  top_module a_top  \n# another\nrun\n"
        assert parse_script_lines(text) == ["read_verilog a.v", "top_module a_top", "run"]

    def test_empty_text_is_an_empty_list(self):
        assert parse_script_lines("") == []

    def test_whitespace_only_line_is_dropped(self):
        assert parse_script_lines("run\n   \nexit\n") == ["run", "exit"]


class TestRunScript:
    """MaceShell.run_script -- the non-interactive `-c script.mace`
    counterpart to cmdloop(), run through onecmd() the same way an
    interactive session's typed commands are."""

    def test_runs_each_line_in_order(self):
        from mace.cli.shell import MaceShell

        session = Session(piton_root="/x")
        shell = MaceShell(session, llm=None, db=None)

        shell.run_script(["top_module ariane_top", "set_core 1"])

        assert session.top_module == "ariane_top"
        assert session.core_count == 1

    def test_stops_early_on_exit(self):
        from mace.cli.shell import MaceShell

        session = Session(piton_root="/x")
        shell = MaceShell(session, llm=None, db=None)

        shell.run_script(["top_module ariane_top", "exit", "top_module sparc_top"])

        assert session.top_module == "ariane_top"  # the line after exit never ran

    def test_an_error_in_one_line_does_not_abort_the_rest(self, capsys):
        from mace.cli.shell import MaceShell

        session = Session(piton_root="/x")
        shell = MaceShell(session, llm=None, db=None)

        shell.run_script(["read_verilog /no/such/file.v", "top_module ariane_top"])

        out = capsys.readouterr().out
        assert "ERROR" in out
        assert session.top_module == "ariane_top"  # execution continued past the error

    def test_a_bracket_shaped_line_does_not_crash_or_get_mangled(self, capsys):
        """run_script echoes each line through Console.print() before
        running it -- a line merely containing a "[...]"-shaped substring
        (a plausible real path like "notes[/legacy].txt") used to either
        raise an uncaught rich.errors.MarkupError (aborting the whole
        script before this or any later line ran) or get silently
        corrupted in the printed echo, even though the real, unmangled
        argument still reached onecmd.
        """
        from mace.cli.shell import MaceShell

        session = Session(piton_root="/x")
        shell = MaceShell(session, llm=None, db=None)

        shell.run_script(["top_module notes[/legacy]", "set_core 1"])

        out = capsys.readouterr().out
        assert "notes[/legacy]" in out
        assert session.top_module == "notes[/legacy]"
        assert session.core_count == 1  # execution reached the line after it


class TestShellScriptOption:
    def test_missing_script_file_fails_fast_before_ray_init(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        env_file = tmp_path / ".env"
        write_env_file("opencode", "sk-test", env_file)

        def fail_if_called(*a, **k):
            raise AssertionError("ray.init must not be reached when the script file is missing")

        monkeypatch.setattr("mace.cli.shell.ray.init", fail_if_called)
        (tmp_path / "piton").mkdir()

        result = CliRunner().invoke(
            app,
            [
                "shell", "--piton-root", str(tmp_path), "--backend", "opencode",
                "--api", str(env_file), "--script", str(tmp_path / "does_not_exist.mace"),
            ],
        )

        assert result.exit_code == 1
        assert "Can't read script file" in result.output

    def test_a_piton_root_without_piton_dir_fails_before_ray_init(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        def fail_if_called(*a, **k):
            raise AssertionError("ray.init must not be reached with a bad --piton-root")

        monkeypatch.setattr("mace.cli.shell.ray.init", fail_if_called)

        result = CliRunner().invoke(app, ["shell", "--piton-root", str(tmp_path)])

        assert result.exit_code == 1
        assert "not an OpenPiton checkout" in result.output


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

    def test_unterminated_quote_is_a_clean_error_not_a_raw_valueerror(self):
        session = Session(piton_root="/x")
        msg = handle_read_verilog(session, '"foo.v')
        assert msg.startswith("ERROR")

    def test_success_message_does_not_claim_the_files_get_built(self, tmp_path):
        # session.verilog_files is validated-only -- build_spec_from_session
        # never reads it -- so the message must not imply `run` will use these
        # files, the way "read N file(s)" (Yosys/OpenROAD phrasing) would.
        f = tmp_path / "core.v"
        f.write_text("module core; endmodule\n")
        session = Session(piton_root="/x")

        msg = handle_read_verilog(session, str(f))

        assert "not wired into the build" in msg

    def test_windows_native_path_backslashes_survive_tokenizing(self, monkeypatch):
        """shlex.split's default POSIX mode treats \\ as an escape
        character, so a native Windows path typed into the shell
        (C:\\Users\\me\\foo.v) used to come out mangled to
        C:Usersmefoo.v -- every backslash silently eaten -- instead of
        being preserved for the exists() check right after.
        """
        from mace.cli.shell import _split_file_args

        monkeypatch.setattr("mace.cli.shell._is_windows", lambda: True)

        tokens = _split_file_args(r"C:\Users\me\foo.v")

        assert tokens == [r"C:\Users\me\foo.v"]

    def test_windows_native_path_with_quotes_still_strips_them(self, monkeypatch):
        from mace.cli.shell import _split_file_args

        monkeypatch.setattr("mace.cli.shell._is_windows", lambda: True)

        tokens = _split_file_args(r'"C:\Program Files\foo.v"')

        assert tokens == [r"C:\Program Files\foo.v"]


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
    @pytest.mark.parametrize("n,mesh", [(4, (2, 2)), (16, (4, 4))])
    def test_sets_a_known_mesh_and_reports_its_outcome(self, n, mesh):
        session = Session(piton_root="/x")
        msg = handle_set_core(session, str(n))
        assert session.target_mesh == mesh
        assert "every tile reaching Hit Good trap" in msg  # KNOWN_MESH_OUTCOMES[n]'s real result

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

    def test_a_count_that_only_factors_into_an_oversized_axis_is_a_clean_error(self):
        """65535 = 255x257 -- one axis over the 256-tile-per-axis limit --
        must reach the same clean ERROR path as `0`, not report a false
        "will attempt it" success that later crashes inside run() when
        chia_openpiton's own PitonConfig validates the axes for real.
        """
        session = Session(piton_root="/x")
        msg = handle_set_core(session, "65535")
        assert msg.startswith("ERROR")
        assert session.core_count is None
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

    def test_ctrl_c_during_run_returns_to_the_prompt_not_the_os(self, monkeypatch):
        """Ctrl-C during a long build/simulate must return to the shell
        prompt with the session intact, not kill the whole process -- see
        MaceShell.onecmd's own comment on why Exception alone doesn't catch
        this (KeyboardInterrupt is a BaseException).
        """
        from mace.cli.shell import MaceShell

        def raising_run_mace_loop(*args, **kwargs):
            raise KeyboardInterrupt()

        monkeypatch.setattr("mace.cli.shell.run_mace_loop", raising_run_mace_loop)

        session = Session(piton_root="/x", top_module="ariane_top")
        shell = MaceShell(session, llm=None, db=None)

        still_running = shell.onecmd("run")

        assert still_running is False  # cmd.Cmd convention: False keeps the loop going
        assert session.top_module == "ariane_top"  # session state survived intact

    def test_ctrl_c_during_run_leaves_last_result_and_last_coverage_consistent(self, monkeypatch):
        """A previous run's last_result/last_coverage must not be split
        apart by an interrupt mid-run. last_coverage used to be cleared up
        front -- before build_spec_from_session/run_mace_loop even ran --
        so a Ctrl-C left last_result stale from the PREVIOUS run while
        last_coverage had already been wiped, even though that previous
        run genuinely had a coverage percentage to report.
        """
        from mace.cli.shell import MaceShell
        from mace.spec import LoopResult

        def raising_run_mace_loop(*args, **kwargs):
            raise KeyboardInterrupt()

        monkeypatch.setattr("mace.cli.shell.run_mace_loop", raising_run_mace_loop)

        session = Session(piton_root="/x", top_module="ariane_top")
        previous_result = LoopResult(run_id="prev", status="passed", iterations=())
        session.last_result = previous_result
        session.last_coverage = {"hit": 100, "total": 200, "percent": 50.0}
        shell = MaceShell(session, llm=None, db=None)

        shell.onecmd("run")

        assert session.last_result is previous_result
        assert session.last_coverage == {"hit": 100, "total": 200, "percent": 50.0}

    def test_task_progress_callback_prints_the_stage_and_task_ids(self, capsys, monkeypatch):
        """Real-time in-flight feedback: do_run must pass a real
        on_task_progress through to run_mace_loop and print what it's told
        -- otherwise a multi-minute build leaves the user staring at a
        silent terminal with no way to tell "working" from "stuck".
        """
        from mace.cli.shell import MaceShell
        from mace.spec import LoopResult

        def fake_run_mace_loop(
            piton_roots, spec, llm, db, tools=(), on_iteration=None, on_task_progress=None
        ):
            on_task_progress(("t1", "t2"), "building")
            return LoopResult(run_id=None, status="failed", iterations=())

        monkeypatch.setattr("mace.cli.shell.run_mace_loop", fake_run_mace_loop)

        session = Session(piton_root="/x", top_module="ariane_top")
        shell = MaceShell(session, llm=None, db=None)

        shell.do_run("")

        out = capsys.readouterr().out
        assert "building" in out
        assert "t1, t2" in out

    def test_verbose_flag_is_acknowledged_not_silently_ignored(self, capsys):
        """-verbose is accepted (for Yosys/OpenROAD familiarity) but has no
        effect -- a user who types it should see that stated plainly, not
        be left guessing whether it was understood.
        """
        from mace.cli.shell import MaceShell

        session = Session(piton_root="/x", top_module="not_a_known_core")
        shell = MaceShell(session, llm=None, db=None)

        shell.do_run("-verbose")

        assert "has no effect" in capsys.readouterr().out

    def test_redirect_syntax_gets_a_specific_error_not_a_generic_one(self, capsys):
        """`>` is real syntax on write_report -- a plausible mistake to
        carry over to run, which doesn't support it. The error must name
        the actual fix, not just say "unknown option".
        """
        from mace.cli.shell import MaceShell

        session = Session(piton_root="/x", top_module="not_a_known_core")
        shell = MaceShell(session, llm=None, db=None)

        shell.do_run("> result.rpt")

        out = capsys.readouterr().out
        assert "doesn't support" in out
        assert "write_report" in out
        assert "unknown run option" not in out

    def test_unrelated_unknown_option_still_gets_the_generic_error(self, capsys):
        from mace.cli.shell import MaceShell

        session = Session(piton_root="/x", top_module="not_a_known_core")
        shell = MaceShell(session, llm=None, db=None)

        shell.do_run("-bogus")

        assert "unknown run option" in capsys.readouterr().out

    def test_unterminated_quote_is_a_clean_error_not_a_raw_valueerror(self, capsys):
        """shlex.split raises a bare ValueError on an unterminated quote --
        previously uncaught here, so it fell through to onecmd's generic
        handler and printed an inconsistent raw "ValueError: ..." message
        instead of this command's own clean ERROR: style.
        """
        from mace.cli.shell import MaceShell

        session = Session(piton_root="/x", top_module="not_a_known_core")
        shell = MaceShell(session, llm=None, db=None)

        shell.do_run('"unterminated')

        out = capsys.readouterr().out
        assert "ERROR" in out


class TestPrompt:
    """The interactive prompt is a raw ANSI escape, not routed through
    Rich (which guards non-tty output on its own) -- it must guard itself.
    """

    def test_non_tty_prompt_has_no_raw_ansi_escape(self):
        from mace.cli.shell import MaceShell

        # pytest's own output capture already makes sys.stdout not a real
        # tty, which is exactly the condition being tested.
        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        assert shell.prompt == "mace> "

    def test_tty_prompt_uses_the_styled_escape(self, monkeypatch):
        import sys as sys_module

        from mace.cli.shell import MaceShell

        monkeypatch.setattr(sys_module.stdout, "isatty", lambda: True)
        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        assert shell.prompt == "\033[1;36mmace> \033[0m"


class TestHistoryPersistence:
    """cmd.Cmd's own readline history is in-session only -- preloop/postloop
    persist it across shell restarts, next to ~/.mace/.env.
    """

    def _fake_readline(self, calls, raise_on_read=False):
        def read_history_file(path):
            if raise_on_read:
                raise OSError("no such file")
            calls.append(("read", path))

        return type(
            "FakeReadline",
            (),
            {
                "read_history_file": staticmethod(read_history_file),
                "write_history_file": staticmethod(lambda path: calls.append(("write", path))),
            },
        )

    def test_preloop_reads_the_history_file(self, monkeypatch, tmp_path):
        from mace.cli.shell import MaceShell

        history_path = tmp_path / ".shell_history"
        calls = []
        monkeypatch.setattr("mace.cli.shell.HISTORY_FILE", history_path)
        monkeypatch.setattr("mace.cli.shell.readline", self._fake_readline(calls))

        MaceShell(Session(piton_root="/x"), llm=None, db=None).preloop()

        assert calls == [("read", history_path)]

    def test_postloop_writes_the_history_file_creating_its_parent(self, monkeypatch, tmp_path):
        from mace.cli.shell import MaceShell

        history_path = tmp_path / "not-yet-created" / ".shell_history"
        calls = []
        monkeypatch.setattr("mace.cli.shell.HISTORY_FILE", history_path)
        monkeypatch.setattr("mace.cli.shell.readline", self._fake_readline(calls))

        MaceShell(Session(piton_root="/x"), llm=None, db=None).postloop()

        assert calls == [("write", history_path)]
        assert history_path.parent.is_dir()

    def test_preloop_tolerates_a_missing_history_file(self, monkeypatch, tmp_path):
        from mace.cli.shell import MaceShell

        monkeypatch.setattr("mace.cli.shell.HISTORY_FILE", tmp_path / "nope" / ".shell_history")
        monkeypatch.setattr("mace.cli.shell.readline", self._fake_readline([], raise_on_read=True))

        MaceShell(Session(piton_root="/x"), llm=None, db=None).preloop()  # must not raise

    def test_no_readline_module_is_a_silent_no_op(self, monkeypatch):
        from mace.cli.shell import MaceShell

        monkeypatch.setattr("mace.cli.shell.readline", None)

        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        shell.preloop()
        shell.postloop()  # neither raises


class TestPathCompletion:
    def test_completes_matching_files(self, tmp_path):
        from mace.cli.shell import _complete_path

        (tmp_path / "core_a.v").write_text("")
        (tmp_path / "core_b.v").write_text("")
        (tmp_path / "other.txt").write_text("")

        matches = _complete_path(str(tmp_path / "core"))

        assert sorted(matches) == sorted(
            [str(tmp_path / "core_a.v"), str(tmp_path / "core_b.v")]
        )

    def test_directories_get_a_trailing_separator(self, tmp_path):
        import os

        from mace.cli.shell import _complete_path

        (tmp_path / "subdir").mkdir()

        matches = _complete_path(str(tmp_path / "sub"))

        assert matches == [str(tmp_path / "subdir") + os.sep]

    def test_no_matches_is_an_empty_list_not_an_error(self, tmp_path):
        from mace.cli.shell import _complete_path

        assert _complete_path(str(tmp_path / "nope_does_not_exist")) == []

    def test_read_verilog_read_spec_and_write_report_all_wire_up_the_completer(
        self, monkeypatch
    ):
        from mace.cli.shell import MaceShell

        monkeypatch.setattr("mace.cli.shell._complete_path", lambda text: [f"{text}-match"])
        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)

        assert shell.complete_read_verilog("foo", "read_verilog foo", 13, 16) == ["foo-match"]
        assert shell.complete_read_spec("foo", "read_spec foo", 10, 13) == ["foo-match"]
        assert shell.complete_write_report("foo", "write_report foo", 13, 16) == ["foo-match"]


class TestDefault:
    """Unknown-command handling -- a generic closest-match suggestion on
    top of the two hand-diagnosed cases (`init`, `mace`) already there.
    """

    def test_close_typo_suggests_the_real_command(self, capsys):
        from mace.cli.shell import MaceShell

        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        shell.onecmd("exi")
        out = capsys.readouterr().out

        assert "did you mean" in out
        assert "exit" in out

    def test_unrelated_input_gets_no_false_suggestion(self, capsys):
        from mace.cli.shell import MaceShell

        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        shell.onecmd("xyzzy_totally_unrelated")
        out = capsys.readouterr().out

        assert "did you mean" not in out
        assert "unknown command" in out

    def test_bracket_shaped_unknown_command_does_not_crash(self, capsys):
        """A mistyped command that happens to contain a "[...]"-shaped
        substring must not crash with an uncaught rich.errors.MarkupError
        -- it's still just an unknown command."""
        from mace.cli.shell import MaceShell

        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        shell.onecmd("frobnicate[legacy]")
        out = capsys.readouterr().out

        assert "unknown command" in out
        assert "[legacy]" in out


class TestDoHelp:
    def test_bare_help_shows_argument_syntax_not_just_descriptions(self, capsys):
        """A user must not have to run `help <command>` individually just
        to learn a command's argument syntax -- the bare listing shows it
        too now, in its own column. See do_help's own comment.
        """
        from mace.cli.shell import MaceShell

        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        shell.do_help("")
        out = capsys.readouterr().out

        assert "-verbose" in out  # run's usage
        assert "<N>" in out  # set_core's usage

    def test_bare_help_does_not_swallow_bracketed_usage_text(self, capsys):
        """read_verilog's own usage is "<file> [file2 ...]" -- unescaped,
        Rich's markup parser silently deletes the "[file2 ...]" part
        instead of printing it literally."""
        from mace.cli.shell import MaceShell

        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        shell.do_help("")
        out = capsys.readouterr().out

        assert "[file2 ...]" in out

    def test_help_for_one_command_does_not_swallow_bracketed_text(self, capsys):
        from mace.cli.shell import MaceShell

        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        shell.do_help("read_verilog")
        out = capsys.readouterr().out

        assert "[file2 ...]" in out


class TestPrintResult:
    """_print_result renders every handle_*() message -- including the
    interactive shell's do_top_module/do_read_verilog/etc, not just script
    mode. A message that echoes back the user's own bracket-shaped
    argument must print literally, not crash or get silently mangled."""

    def test_bracket_shaped_top_module_name_does_not_crash(self, capsys):
        from mace.cli.shell import MaceShell

        shell = MaceShell(Session(piton_root="/x"), llm=None, db=None)
        shell.onecmd("top_module notes[/legacy]")
        out = capsys.readouterr().out

        assert "notes[/legacy]" in out
        assert shell.session.top_module == "notes[/legacy]"


class TestFormatReport:
    def test_no_run_yet(self):
        text = format_report(Session(piton_root="/x"))
        assert "no run has happened yet" in text

    def test_no_adapter_static_result_gets_the_friendly_run_id_placeholder(self):
        """The no-adapter path's static result sets run_id=None explicitly
        (present, not missing) -- getattr's own default only fires when an
        attribute is absent, so this needs its own None check or the report
        prints the literal "run_id: None" instead.
        """
        session = Session(piton_root="/x", top_module="not_a_known_core")
        pm = no_adapter_post_mortem(session)
        session.last_result = type(
            "StaticResult", (), {"run_id": None, "status": "no_adapter", "post_mortem": pm}
        )()

        text = format_report(session)

        assert "run_id: None" not in text
        assert "static check, no real run" in text

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
