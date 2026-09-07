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
