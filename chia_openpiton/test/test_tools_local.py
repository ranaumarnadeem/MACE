"""Tier-0 tests for PitonToolServer: no Ray needed.

Run:
    pytest chia_openpiton/test/test_tools_local.py -q

Two things make this possible without a live cluster:

* Bad ``expose=`` values raise in ``__init__`` *before* ``super().__post_init__()``
  ever touches Ray, so real construction can be tested for that path alone.
* Everything else is tested on a BARE instance built with
  ``object.__new__(PitonToolServer)`` plus the handful of attributes each method
  actually reads -- never calling ``ChiaTool.__init__``/``__post_init__`` at all.
  This is legitimate here because ``AsyncJobTool``'s job machinery is plain
  ``threading``, not a Ray actor: only the MCP server deployment needs Ray, and
  none of these tests touch it. ``_job_start`` even lazily initializes its own
  threading state on first use (see ``AsyncJobTool._ensure_job_state``), so a
  bare instance's build()/run()/job_status() work exactly like a real tool's.
"""

from __future__ import annotations

import os
import time

import pytest

from chia_openpiton.state_def import PitonConfig, PitonRunResult
from chia_openpiton.tools import PitonToolServer, _grep_lines, _render_config


def bare_tool(piton_root: str, config: PitonConfig, **extra) -> PitonToolServer:
    """A PitonToolServer with no MCP server, no Ray, no actor -- just the
    attributes its methods read."""
    tool = object.__new__(PitonToolServer)
    tool.name = "piton"
    tool.piton_root = piton_root
    tool.asm_diag_root = extra.get("asm_diag_root")
    tool.build_timeout_s = extra.get("build_timeout_s", 60)
    tool.run_timeout_s = extra.get("run_timeout_s", 60)
    tool._config = config
    tool._last_build = None
    tool._last_run = extra.get("last_run")
    return tool


@pytest.fixture
def cfg():
    return PitonConfig(core="sparc", source_rev="deadbeef", verilator_version="Verilator 4.014")


class TestExposeValidation:
    """The one thing that needs a real (Ray-touching) construction path --
    and only because it must prove the check runs BEFORE that path."""

    def test_unknown_tool_name_raises_before_touching_ray(self, stub_piton_root, cfg):
        with pytest.raises(ValueError, match="unknown tool"):
            PitonToolServer(
                "piton", str(stub_piton_root), cfg,
                expose=("build", "nonsense"), task_options=None,
            )

    def test_error_lists_the_valid_names(self, stub_piton_root, cfg):
        with pytest.raises(ValueError, match="config_get"):
            PitonToolServer("piton", str(stub_piton_root), cfg, expose=("nope",))


class TestBuildJob:
    def test_build_reports_success_from_the_node(self, stub_piton_root, cfg, sims_argv):
        tool = bare_tool(str(stub_piton_root), cfg)
        tool.build()
        deadline = time.time() + 10
        result = tool.job_status(wait_seconds=1)
        while not result["done"] and time.time() < deadline:
            result = tool.job_status(wait_seconds=1)
        assert result["done"] is True
        assert result["job_type"] == "build"
        assert result["success"] is True
        assert tool._last_build is not None and tool._last_build.success

    def test_failed_build_is_reported_not_raised(self, stub_piton_root, cfg, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_FAIL_BUILD", "1")
        tool = bare_tool(str(stub_piton_root), cfg)
        tool.build()
        deadline = time.time() + 10
        result = tool.job_status(wait_seconds=1)
        while not result["done"] and time.time() < deadline:
            result = tool.job_status(wait_seconds=1)
        assert result["success"] is False
        assert result["failure_reason"]

    def test_config_set_after_dispatch_does_not_change_the_running_build(
        self, stub_piton_root, cfg, sims_argv
    ):
        """build() snapshots the config before starting the thread."""
        tool = bare_tool(str(stub_piton_root), cfg)
        tool.build()
        tool.config_set(x_tiles=4)  # must not retarget the in-flight job
        deadline = time.time() + 10
        result = tool.job_status(wait_seconds=1)
        while not result["done"] and time.time() < deadline:
            result = tool.job_status(wait_seconds=1)
        assert result["success"] is True
        assert tool._last_build.config.x_tiles == 1  # the ORIGINAL config, not 4

    def test_second_build_while_one_is_running_is_refused(
        self, stub_piton_root, cfg, monkeypatch
    ):
        monkeypatch.setenv("FAKE_SIMS_SLEEP", "2")
        tool = bare_tool(str(stub_piton_root), cfg)
        first = tool.build()
        assert first["started"] is True
        second = tool.build()
        assert second["started"] is False
        assert second["running"] is True

    def test_job_status_before_anything_started(self, stub_piton_root, cfg):
        tool = bare_tool(str(stub_piton_root), cfg)
        result = tool.job_status(wait_seconds=1)
        assert result == {"done": False, "running": False, "note": "nothing started yet"}


class TestRunJob:
    def test_run_verdict_reaches_job_status(self, stub_piton_root, cfg, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        tool = bare_tool(str(stub_piton_root), cfg)
        tool.build()
        deadline = time.time() + 10
        while not tool.job_status(wait_seconds=1)["done"] and time.time() < deadline:
            pass
        tool.run("hello_world.c")
        deadline = time.time() + 10
        result = tool.job_status(wait_seconds=1)
        while not result["done"] and time.time() < deadline:
            result = tool.job_status(wait_seconds=1)
        assert result["job_type"] == "run"
        assert result["verdict"] == "pass"
        assert result["success"] is True


class TestGrep:
    def _run_result(self, tmp_path, sim_log="", status_log="", fake_uart=""):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "sim.log").write_text(sim_log)
        (run_dir / "status.log").write_text(status_log)
        (run_dir / "fake_uart.log").write_text(fake_uart)
        return PitonRunResult(
            success=True, returncode=0, test="t", sim_type="vlt", run_dir=str(run_dir)
        )

    def test_no_run_yet(self, stub_piton_root, cfg):
        tool = bare_tool(str(stub_piton_root), cfg)
        assert "no run yet" in tool.grep("sim_log", "x")

    def test_matches_with_context(self, stub_piton_root, cfg, tmp_path):
        log = "\n".join(f"line{i}" for i in range(10)).replace("line5", "line5 ERROR")
        tool = bare_tool(str(stub_piton_root), cfg,
                         last_run=self._run_result(tmp_path, sim_log=log))
        out = tool.grep("sim_log", "ERROR", context=1)
        assert "line4" in out and "line5 ERROR" in out and "line6" in out
        assert "line0" not in out

    def test_unknown_source_is_an_error_not_an_exception(self, stub_piton_root, cfg, tmp_path):
        tool = bare_tool(str(stub_piton_root), cfg, last_run=self._run_result(tmp_path))
        assert "ERROR" in tool.grep("nonsense_log", "x")

    def test_bad_regex_is_an_error_not_an_exception(self, stub_piton_root, cfg, tmp_path):
        tool = bare_tool(str(stub_piton_root), cfg, last_run=self._run_result(tmp_path))
        assert "ERROR: bad regex" in tool.grep("sim_log", "(unclosed")

    def test_no_match_says_so(self, stub_piton_root, cfg, tmp_path):
        tool = bare_tool(str(stub_piton_root), cfg,
                         last_run=self._run_result(tmp_path, sim_log="all fine here"))
        assert "no lines match" in tool.grep("sim_log", "ERROR")

    def test_fake_uart_is_readable(self, stub_piton_root, cfg, tmp_path):
        tool = bare_tool(str(stub_piton_root), cfg,
                         last_run=self._run_result(tmp_path, fake_uart="Hello from the program\n"))
        assert "Hello from the program" in tool.grep("fake_uart", "Hello")


class TestCollect:
    def test_no_run_yet(self, stub_piton_root, cfg):
        tool = bare_tool(str(stub_piton_root), cfg)
        assert "no run yet" in tool.collect()

    def test_files_are_returned_inline(self, stub_piton_root, cfg, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "status.log").write_text("Diag: t PASS")
        tool = bare_tool(str(stub_piton_root), cfg, last_run=PitonRunResult(
            success=True, returncode=0, test="t", sim_type="vlt", run_dir=str(run_dir)))
        out = tool.collect("*.log")
        assert "status.log" in out and "Diag: t PASS" in out

    def test_oversized_files_are_listed_not_inlined(self, stub_piton_root, cfg, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "big.log").write_text("x" * 1000)
        tool = bare_tool(str(stub_piton_root), cfg, last_run=PitonRunResult(
            success=True, returncode=0, test="t", sim_type="vlt", run_dir=str(run_dir)))
        out = tool.collect("*.log", max_bytes=10)
        assert "skipped" in out and "x" * 1000 not in out


class TestSetContext:
    def test_points_grep_and_collect_at_the_given_run(self, stub_piton_root, cfg, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "sim.log").write_text("Simulation -> PASS (HIT GOOD TRAP)")
        run = PitonRunResult(success=True, returncode=0, test="t", sim_type="vlt", run_dir=str(run_dir))

        tool = bare_tool(str(stub_piton_root), cfg)
        assert "no run yet" in tool.grep("sim_log", "x")

        tool.set_context(None, run)

        assert "PASS" in tool.grep("sim_log", "PASS")


class TestCompareToFixture:
    def _run_result(self, tmp_path, sim_log: str) -> PitonRunResult:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "sim.log").write_text(sim_log)
        return PitonRunResult(success=True, returncode=0, test="t", sim_type="vlt", run_dir=str(run_dir))

    def test_no_run_yet(self, stub_piton_root, cfg):
        tool = bare_tool(str(stub_piton_root), cfg)
        assert "no run yet" in tool.compare_to_fixture("run_pass_sim.log")

    def test_unknown_fixture_name_is_an_error(self, stub_piton_root, cfg, tmp_path):
        tool = bare_tool(str(stub_piton_root), cfg, last_run=self._run_result(tmp_path, "x"))
        out = tool.compare_to_fixture("no_such_fixture.log")
        assert "ERROR" in out and "unknown fixture" in out

    def test_path_traversal_is_rejected(self, stub_piton_root, cfg, tmp_path):
        """fixture_name is a model-supplied string -- it must not be able to
        read anything outside chia_openpiton/test/fixtures/."""
        tool = bare_tool(str(stub_piton_root), cfg, last_run=self._run_result(tmp_path, "x"))
        out = tool.compare_to_fixture("../../../../etc/passwd")
        assert "ERROR" in out and "unknown fixture" in out

    def test_identical_to_fixture_says_so(self, stub_piton_root, cfg, tmp_path, fixtures):
        tool = bare_tool(
            str(stub_piton_root), cfg,
            last_run=self._run_result(tmp_path, fixtures("run_pass_sim.log")),
        )
        out = tool.compare_to_fixture("run_pass_sim.log")
        assert "identical to run_pass_sim.log" in out

    def test_reports_the_real_divergence_point(self, stub_piton_root, cfg, tmp_path, fixtures):
        """The exact real-world use case (see docs/TECHNICAL_GUIDE.md's
        PicoRV32/2x2-mesh findings): a maxcycles run diverging from the
        known-good passing transcript."""
        tool = bare_tool(
            str(stub_piton_root), cfg,
            last_run=self._run_result(tmp_path, fixtures("run_maxcycles_sim.log")),
        )
        out = tool.compare_to_fixture("run_pass_sim.log")
        assert "diverges from run_pass_sim.log at line" in out
        assert "this run's sim.log" in out


class TestSymbolCheck:
    def _run_result(self, tmp_path, with_binary=True, with_symtbl=True) -> PitonRunResult:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        if with_binary:
            (run_dir / "diag.exe").write_bytes(b"\x7fELF")  # content irrelevant; objdump is mocked
        if with_symtbl:
            (run_dir / "symbol.tbl").write_text("good_trap 0000000080000540 X 0000000080000540\n")
        return PitonRunResult(success=True, returncode=0, test="t", sim_type="vlt", run_dir=str(run_dir))

    def _fake_objdump(self, monkeypatch, f_stdout: str, t_stdout: str):
        from types import SimpleNamespace

        def fake(cmd, **kwargs):
            stdout = f_stdout if "-f" in cmd else t_stdout
            return SimpleNamespace(stdout=stdout, returncode=0)

        monkeypatch.setattr("chia_openpiton.tools.subprocess.run", fake)

    def test_no_run_yet(self, stub_piton_root, cfg):
        tool = bare_tool(str(stub_piton_root), cfg)
        assert "no run yet" in tool.symbol_check()

    def test_no_binary_in_run_dir(self, stub_piton_root, cfg, tmp_path):
        tool = bare_tool(
            str(stub_piton_root), cfg,
            last_run=self._run_result(tmp_path, with_binary=False),
        )
        assert "no diag.exe" in tool.symbol_check()

    def test_missing_objdump_is_an_error_not_an_exception(self, stub_piton_root, cfg, tmp_path, monkeypatch):
        def raise_not_found(cmd, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "objdump")

        monkeypatch.setattr("chia_openpiton.tools.subprocess.run", raise_not_found)
        tool = bare_tool(str(stub_piton_root), cfg, last_run=self._run_result(tmp_path))

        out = tool.symbol_check()

        assert "ERROR" in out and "objdump" in out

    def test_real_captured_objdump_output_and_symbol_tbl_shown_side_by_side(
        self, stub_piton_root, cfg, tmp_path, fixtures, monkeypatch
    ):
        """Uses real objdump -f/-t output captured from an actual passing
        run on this machine (chia_openpiton/test/fixtures/objdump_{f,t}_pass.txt),
        proving the tool's formatting handles real data, not just synthetic
        strings."""
        self._fake_objdump(
            monkeypatch, fixtures("objdump_f_pass.txt"), fixtures("objdump_t_pass.txt")
        )
        tool = bare_tool(str(stub_piton_root), cfg, last_run=self._run_result(tmp_path))

        out = tool.symbol_check()

        assert "start address 0x0000000080000000" in out
        assert "good_trap" in out  # from symbol.tbl
        assert "pass" in out  # the real objdump symbol at the same address

    def test_objdump_f_failure_is_surfaced_as_an_error_not_empty_output(
        self, stub_piton_root, cfg, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace

        def fake(cmd, **kwargs):
            if "-f" in cmd:
                return SimpleNamespace(stdout="", stderr="objdump: not an object file\n", returncode=1)
            return SimpleNamespace(stdout="SYMBOL TABLE:\n", stderr="", returncode=0)

        monkeypatch.setattr("chia_openpiton.tools.subprocess.run", fake)
        tool = bare_tool(str(stub_piton_root), cfg, last_run=self._run_result(tmp_path))

        out = tool.symbol_check()

        assert "ERROR" in out
        assert "objdump: not an object file" in out

    def test_objdump_t_failure_is_surfaced_as_an_error_not_empty_output(
        self, stub_piton_root, cfg, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace

        def fake(cmd, **kwargs):
            if "-t" in cmd:
                return SimpleNamespace(stdout="", stderr="objdump: not an object file\n", returncode=1)
            return SimpleNamespace(stdout="start address 0x80000000\n", stderr="", returncode=0)

        monkeypatch.setattr("chia_openpiton.tools.subprocess.run", fake)
        tool = bare_tool(str(stub_piton_root), cfg, last_run=self._run_result(tmp_path))

        out = tool.symbol_check()

        assert "ERROR" in out
        assert "objdump: not an object file" in out

    def test_missing_symbol_tbl_says_so_but_still_shows_objdump(
        self, stub_piton_root, cfg, tmp_path, monkeypatch
    ):
        self._fake_objdump(monkeypatch, "start address 0x80000000\n", "SYMBOL TABLE:\n")
        tool = bare_tool(
            str(stub_piton_root), cfg,
            last_run=self._run_result(tmp_path, with_symtbl=False),
        )

        out = tool.symbol_check()

        assert "start address 0x80000000" in out
        assert "not found in this run directory" in out


class TestConfig:
    def test_config_get_renders_the_current_config(self, stub_piton_root, cfg):
        tool = bare_tool(str(stub_piton_root), cfg)
        out = tool.config_get()
        assert "core=sparc" in out and "mesh=1x1" in out

    def test_config_set_changes_only_the_given_fields(self, stub_piton_root, cfg):
        tool = bare_tool(str(stub_piton_root), cfg)
        tool.config_set(x_tiles=2)
        assert tool._config.x_tiles == 2
        assert tool._config.y_tiles == 1  # unspecified: preserved
        assert tool._config.core == "sparc"

    def test_config_set_gives_a_fresh_build_id(self, stub_piton_root, cfg):
        tool = bare_tool(str(stub_piton_root), cfg)
        before = tool._config.build_id
        tool.config_set(x_tiles=2)
        assert tool._config.build_id != before

    def test_config_set_returns_confirmation_text(self, stub_piton_root, cfg):
        tool = bare_tool(str(stub_piton_root), cfg)
        out = tool.config_set(core="ariane")
        assert "OK" in out and "core=ariane" in out


def test_render_config_is_a_pure_function():
    text = _render_config(PitonConfig(x_tiles=2, y_tiles=2))
    assert "mesh=2x2" in text


def test_grep_lines_caps_output():
    text = "\n".join(f"match {i}" for i in range(100))
    out = _grep_lines(text, "match", context=0, max_lines=5)
    assert len(out.splitlines()) == 5
