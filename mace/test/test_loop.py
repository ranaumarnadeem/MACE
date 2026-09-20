"""Tier-0 tests for mace.loop.run_mace_step.

Run:
    pytest mace/test/test_loop.py -q

The smallest possible real loop: one task, no fan-out, no Planner. These
tests prove the wiring -- LLM dispatch, build, run, gate on the transcript
verdict -- against the real OpenPitonWorkspaceNode machinery (a stubbed
`sims`, exactly as chia_openpiton's own tests use), not against anything
Ray/network/cost-bearing.
"""

from __future__ import annotations

from chia_openpiton.state_def import PitonConfig
from mace.loop import run_mace_step
from mace.spec import MaceSpec, Task
from mace.test.conftest import FakeLLM

TASK = Task(id="t1", deps=(), kind="workload", spec="run hello_world.c and report the result")


def make_spec(**override):
    kwargs = {"workloads": ("hello_world.c",), "objective": "bring up 1x1 ariane"}
    kwargs.update(override)
    return MaceSpec(**kwargs)


class TestHappyPath:
    def test_passing_verdict_marks_the_step_passed(self, stub_piton_root, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edited the config"])

        result = run_mace_step(str(stub_piton_root), make_spec(), TASK, llm)

        assert result.build.success is True
        assert result.run.verdict == "pass"
        assert result.passed is True

    def test_query_result_is_carried_through(self, stub_piton_root, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["did the edit"])

        result = run_mace_step(str(stub_piton_root), make_spec(), TASK, llm)

        assert result.query.result == "did the edit"
        assert result.task is TASK


class TestFailingVerdicts:
    def test_bad_trap_builds_but_does_not_pass(self, stub_piton_root, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "fail")
        llm = FakeLLM(responses=["edit"])

        result = run_mace_step(str(stub_piton_root), make_spec(), TASK, llm)

        assert result.build.success is True
        assert result.run.verdict == "fail"
        assert result.passed is False

    def test_timeout_does_not_pass(self, stub_piton_root, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "timeout")
        llm = FakeLLM(responses=["edit"])

        result = run_mace_step(str(stub_piton_root), make_spec(), TASK, llm)

        assert result.passed is False


class TestBuildFailure:
    def test_build_failure_short_circuits_before_run(self, stub_piton_root, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_FAIL_BUILD", "1")
        llm = FakeLLM(responses=["edit that breaks the build"])

        result = run_mace_step(str(stub_piton_root), make_spec(), TASK, llm)

        assert result.build.success is False
        assert result.run is None
        assert result.passed is False


class TestDispatch:
    def test_llm_is_prompted_with_the_task_spec_and_tools(self, stub_piton_root, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit"])
        sentinel_tool = object()

        run_mace_step(str(stub_piton_root), make_spec(), TASK, llm, tools=[sentinel_tool])

        assert llm.calls == [(TASK.spec, (sentinel_tool,))]

    def test_defaults_to_no_tools(self, stub_piton_root, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit"])

        run_mace_step(str(stub_piton_root), make_spec(), TASK, llm)

        assert llm.calls == [(TASK.spec, ())]


class TestConfigFromSpec:
    def test_mesh_and_core_come_from_the_spec(self, stub_piton_root, monkeypatch, sims_argv):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit"])
        spec = make_spec(core="sparc", target_mesh=(2, 1))

        run_mace_step(str(stub_piton_root), spec, TASK, llm)

        build_argv = sims_argv.lines()[0]
        assert "-x_tiles=2" in build_argv
        assert "-y_tiles=1" in build_argv
        assert "-ariane" not in build_argv

    def test_run_passes_an_explicit_rtl_timeout(self, stub_piton_root, monkeypatch, sims_argv):
        """Real hardware needs more than sims' own default (see mace.workloads.
        RECOMMENDED_RTL_TIMEOUT) -- the stub ignores timing entirely, so this
        is the argv-shape check that would have caught the gap without
        needing a real run."""
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit"])

        run_mace_step(str(stub_piton_root), make_spec(), TASK, llm)

        run_argv = sims_argv.lines()[1]
        assert "-rtl_timeout=" in run_argv

    def test_run_defaults_asm_diag_root_to_mace_workloads(
        self, stub_piton_root, monkeypatch, sims_argv
    ):
        """Without this, a spec naming one of mace/workloads/*.c would never
        be found by sims -- only OpenPiton-native diag names would resolve."""
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit"])

        run_mace_step(str(stub_piton_root), make_spec(), TASK, llm)

        run_argv = sims_argv.lines()[1]
        assert "-asm_diag_root=" in run_argv
        assert "mace" in run_argv and "workloads" in run_argv

    def test_run_asm_diag_root_is_overridable(self, stub_piton_root, monkeypatch, sims_argv):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit"])

        run_mace_step(str(stub_piton_root), make_spec(), TASK, llm, asm_diag_root="/somewhere/else")

        run_argv = sims_argv.lines()[1]
        assert "-asm_diag_root=/somewhere/else" in run_argv

    def test_task_cache_override_reaches_the_real_build(self, stub_piton_root, monkeypatch, sims_argv):
        """The exact gap a real run (runs/mace_end_to_end.db, run 4cf5f6027d78)
        hit in production: a task whose spec text asked for an undersized L1D
        built and ran against the *default* cache the whole time, because
        nothing threaded the override from the task into the build's
        PitonConfig. This proves that override now actually reaches sims."""
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit"])
        task_with_override = Task(
            id="t1", deps=(), kind="config", spec="build with a tiny L1D",
            caches=(("l1d", (128, 1)),),
        )

        run_mace_step(str(stub_piton_root), make_spec(), task_with_override, llm)

        build_argv = sims_argv.lines()[0]
        assert "-config_l1d_size=128" in build_argv
        assert "-config_l1d_associativity=1" in build_argv

    def test_task_with_no_cache_override_keeps_the_mesh_default(
        self, stub_piton_root, monkeypatch, sims_argv
    ):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit"])

        run_mace_step(str(stub_piton_root), make_spec(), TASK, llm)

        build_argv = sims_argv.lines()[0]
        assert "-config_l1d_size=8192" in build_argv
        assert "-config_l1d_associativity=4" in build_argv

    def test_spec_coverage_flag_reaches_the_real_build(self, stub_piton_root, monkeypatch, sims_argv):
        """Mirrors test_task_cache_override_reaches_the_real_build: spec.coverage
        only matters if it actually reaches the build's argv, not just the
        PitonConfig object -- this proves COVERAGE_LINE_FLAG gets there."""
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit"])
        spec = make_spec(coverage=True)

        run_mace_step(str(stub_piton_root), spec, TASK, llm)

        build_argv = sims_argv.lines()[0]
        assert "-vlt_build_args=--coverage-line" in build_argv


class TestUnitTestTask:
    """kind='unit_test' takes a different path: scaffold, prompt the agent
    to reconcile ports, build -- never run (see mace.loop._run_unit_test_step
    docstring for why)."""

    def _scaffold(self, stub_piton_root, env_name="design_foo_ut"):
        env_dir = stub_piton_root / "piton" / "verif" / "env" / env_name
        env_dir.mkdir(parents=True)
        return env_dir

    def _rtl_module(self, stub_piton_root, rel_path="design/foo.v"):
        rtl = stub_piton_root / rel_path
        rtl.parent.mkdir(parents=True, exist_ok=True)
        rtl.write_text("module foo (\n  input clk,\n  output reg done\n);\nendmodule\n")
        return rel_path

    def test_builds_against_the_scaffolded_sys_not_manycore(
        self, stub_piton_root, monkeypatch, sims_argv
    ):
        self._scaffold(stub_piton_root)
        rel_path = self._rtl_module(stub_piton_root)
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["reconciled the ports"])
        task = Task(id="t1", deps=(), kind="unit_test", spec=rel_path)

        result = run_mace_step(str(stub_piton_root), make_spec(), task, llm)

        build_argv = sims_argv.lines()[0]
        assert "-sys=design_foo_ut" in build_argv
        assert result.build.success is True

    def test_never_runs_even_on_a_passing_build(self, stub_piton_root, monkeypatch):
        self._scaffold(stub_piton_root)
        rel_path = self._rtl_module(stub_piton_root)
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["reconciled the ports"])
        task = Task(id="t1", deps=(), kind="unit_test", spec=rel_path)

        result = run_mace_step(str(stub_piton_root), make_spec(), task, llm)

        assert result.run is None
        assert result.passed is True  # gated on build.success, not a run verdict

    def test_passed_is_false_when_the_build_fails(self, stub_piton_root, monkeypatch):
        self._scaffold(stub_piton_root)
        rel_path = self._rtl_module(stub_piton_root)
        monkeypatch.setenv("FAKE_SIMS_FAIL_BUILD", "1")
        llm = FakeLLM(responses=["reconciled the ports"])
        task = Task(id="t1", deps=(), kind="unit_test", spec=rel_path)

        result = run_mace_step(str(stub_piton_root), make_spec(), task, llm)

        assert result.build.success is False
        assert result.run is None
        assert result.passed is False

    def test_llm_prompt_includes_the_real_port_names(self, stub_piton_root, monkeypatch):
        self._scaffold(stub_piton_root)
        rel_path = self._rtl_module(stub_piton_root)
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["reconciled the ports"])
        task = Task(id="t1", deps=(), kind="unit_test", spec=rel_path)

        run_mace_step(str(stub_piton_root), make_spec(), task, llm)

        prompt_used = llm.calls[0][0]
        assert "clk" in prompt_used
        assert "done" in prompt_used
        assert "design_foo_ut" in prompt_used

    def test_missing_module_file_does_not_crash_the_step(self, stub_piton_root, monkeypatch):
        self._scaffold(stub_piton_root, env_name="nonexistent_foo_ut")
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["reconciled the ports"])
        task = Task(id="t1", deps=(), kind="unit_test", spec="nonexistent/foo.v")

        result = run_mace_step(str(stub_piton_root), make_spec(), task, llm)

        assert "could not read real ports" in llm.calls[0][0]
        assert result.build.success is True  # build still proceeds regardless

    def test_scaffolding_is_idempotent_across_two_tasks(self, stub_piton_root, monkeypatch):
        env_dir = self._scaffold(stub_piton_root)
        rel_path = self._rtl_module(stub_piton_root)
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["edit 1", "edit 2"])
        task = Task(id="t1", deps=(), kind="unit_test", spec=rel_path)

        run_mace_step(str(stub_piton_root), make_spec(), task, llm)
        run_mace_step(str(stub_piton_root), make_spec(), task, llm)

        assert env_dir.is_dir()  # still there, no error from a second scaffold attempt
