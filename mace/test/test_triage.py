"""Tier-0 tests for mace.triage.

Run:
    pytest mace/test/test_triage.py -q
"""

from __future__ import annotations

import pytest

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace.spec import StepResult, Task
from mace.test.conftest import FakeLLM
from mace.triage import TriageError, build_prompt, triage


def make_failed_result(build_success=True, verdict="fail"):
    cfg = PitonConfig()
    build = PitonBuildArtifact(
        success=build_success, returncode=0 if build_success else 1, config=cfg,
        sim_type="vlt", model_dir="/x", binary_path="/x/Vcmp_top" if build_success else "",
        wall_time_s=1.0, failure_reason="" if build_success else "verilator_bad_option",
        stderr="" if build_success else "%Error: bad flag",
    )
    run = None
    if build_success:
        run = PitonRunResult(
            success=False, returncode=0, test="hello_world.c", sim_type="vlt",
            run_dir="/x/runs/1", verdict=verdict,
            sim_log_tail="1234 : Simulation -> FAIL(TIMEOUT)",
            status_log="Diag: hello_world.c Timeout (TIMEOUT)",
        )
    task = Task(id="t1", deps=(), kind="workload", spec="hello_world.c")
    query = FakeLLM(responses=["edit"]).prompt("edit")
    return StepResult(task=task, query=query, build=build, run=run, passed=False)


class TestBuildPrompt:
    def test_includes_task_and_verdict(self):
        prompt = build_prompt(make_failed_result(verdict="timeout"))
        assert "t1" in prompt
        assert "hello_world.c" in prompt
        assert "timeout" in prompt

    def test_build_failure_includes_stderr_not_run_log(self):
        prompt = build_prompt(make_failed_result(build_success=False))
        assert "verilator_bad_option" in prompt
        assert "%Error: bad flag" in prompt


class TestTriage:
    def test_returns_parsed_diagnosis_and_fix(self):
        llm = FakeLLM(responses=["DIAGNOSIS: timeout\nFIX: raise rtl_timeout\n"])
        result = triage(make_failed_result(), llm)
        assert (result.diagnosis, result.fix) == ("timeout", "raise rtl_timeout")

    def test_dispatches_with_the_built_prompt_and_tools(self):
        llm = FakeLLM(responses=["DIAGNOSIS: timeout\nFIX: x\n"])
        sentinel_tool = object()
        failed = make_failed_result()

        triage(failed, llm, tools=[sentinel_tool])

        message, tools = llm.calls[0]
        assert message == build_prompt(failed)
        assert tools == (sentinel_tool,)

    def test_missing_fix_defaults_to_empty_string(self):
        llm = FakeLLM(responses=["DIAGNOSIS: timeout\n"])
        result = triage(make_failed_result(), llm)
        assert result.fix == ""

    def test_no_diagnosis_line_raises(self):
        llm = FakeLLM(responses=["I'm not sure what happened."])
        with pytest.raises(TriageError, match="no DIAGNOSIS:"):
            triage(make_failed_result(), llm)
