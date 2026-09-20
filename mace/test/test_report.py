"""Tier-0 tests for mace.report.

Run:
    pytest mace/test/test_report.py -q
"""

from __future__ import annotations

import pytest

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace.spec import MaceSpec, StepResult, Task, Triage
from mace.test.conftest import FakeLLM
from mace.report import ReportError, build_prompt, generate_post_mortem


def make_spec(**override):
    kwargs = {"workloads": ("hello_world.c",), "objective": "bring up a 2x2 mesh", "core": "pico"}
    kwargs.update(override)
    return MaceSpec(**kwargs)


def make_result(task_id="t1", verdict="maxcycles", build_success=True):
    cfg = PitonConfig(core="pico")
    build = PitonBuildArtifact(
        success=build_success, returncode=0 if build_success else 1, config=cfg,
        sim_type="vlt", model_dir="/x", binary_path="/x/Vcmp_top" if build_success else "",
        wall_time_s=1.0,
    )
    run = None
    if build_success:
        run = PitonRunResult(
            success=False, returncode=0, test="addi.S", sim_type="vlt",
            run_dir="/x/runs/1", verdict=verdict,
            sim_log_tail="reached max cycles", status_log="",
        )
    task = Task(id=task_id, deps=(), kind="workload", spec="addi.S")
    query = FakeLLM(responses=["edit"]).prompt("edit")
    return StepResult(task=task, query=query, build=build, run=run, passed=False)


class TestBuildPrompt:
    def test_includes_objective_core_and_mesh(self):
        spec = make_spec(target_mesh=(1, 1))
        prompt = build_prompt(spec, ((make_result(),),), (None,), "budget_exceeded")
        assert "bring up a 2x2 mesh" in prompt
        assert "pico" in prompt
        assert "1x1" in prompt
        assert "budget_exceeded" in prompt

    def test_includes_every_task_across_every_iteration(self):
        spec = make_spec()
        iterations = ((make_result("t1"),), (make_result("t2", verdict="fail"),))
        prompt = build_prompt(spec, iterations, (None, None), "budget_exceeded")
        assert "t1" in prompt
        assert "t2" in prompt
        assert "maxcycles" in prompt
        assert "verdict=fail" in prompt

    def test_includes_a_diagnosis_when_one_is_aligned_to_that_iteration(self):
        spec = make_spec()
        diagnosis = Triage(diagnosis="rtl_suspect", fix="try a different mesh")
        prompt = build_prompt(spec, ((make_result(),),), (("t1", diagnosis),), "budget_exceeded")
        assert "rtl_suspect" in prompt
        assert "try a different mesh" in prompt

    def test_diagnosis_is_not_attached_to_a_different_task_in_the_same_iteration(self):
        """Only the first failure in a level is ever triaged (see
        mace.orchestrator's own docstring) -- a passing task, or a second,
        undiagnosed failure, sharing that iteration must not be misreported
        as having this diagnosis too.
        """
        spec = make_spec()
        diagnosis = Triage(diagnosis="rtl_suspect", fix="try a different mesh")
        iterations = ((make_result("t1"), make_result("t2", verdict="fail")),)
        prompt = build_prompt(spec, iterations, (("t1", diagnosis),), "budget_exceeded")

        assert prompt.count("triaged as: rtl_suspect") == 1
        # ... and it's attached between t1's own line and t2's, not t2's.
        assert prompt.index("task t1") < prompt.index("triaged as:") < prompt.index("task t2")

    def test_no_tasks_ever_ran_is_stated_plainly(self):
        spec = make_spec()
        prompt = build_prompt(spec, (), (), "failed")
        assert "no tasks ever ran" in prompt


class TestGeneratePostMortem:
    def test_returns_parsed_assessment_explanation_and_next_steps(self):
        llm = FakeLLM(
            responses=[
                "ASSESSMENT: likely_hardware_limitation\n"
                "EXPLANATION: the core never reaches its own trap address\n"
                "NEXT_STEPS: waveform tracing\n"
            ]
        )
        pm = generate_post_mortem(
            make_spec(), ((make_result(),),), (None,), "budget_exceeded", llm
        )
        assert pm.assessment == "likely_hardware_limitation"
        assert pm.explanation == "the core never reaches its own trap address"
        assert pm.next_steps == "waveform tracing"

    def test_dispatches_with_the_built_prompt_and_tools(self):
        llm = FakeLLM(responses=["ASSESSMENT: inconclusive\n"])
        sentinel_tool = object()
        spec = make_spec()
        iterations = ((make_result(),),)
        diagnoses = (None,)

        generate_post_mortem(spec, iterations, diagnoses, "failed", llm, tools=[sentinel_tool])

        message, tools = llm.calls[0]
        assert message == build_prompt(spec, iterations, diagnoses, "failed")
        assert tools == (sentinel_tool,)

    def test_missing_explanation_and_next_steps_default_to_empty_string(self):
        llm = FakeLLM(responses=["ASSESSMENT: inconclusive\n"])
        pm = generate_post_mortem(make_spec(), ((make_result(),),), (None,), "failed", llm)
        assert (pm.explanation, pm.next_steps) == ("", "")

    def test_no_assessment_line_raises(self):
        llm = FakeLLM(responses=["I'm not sure what to conclude."])
        with pytest.raises(ReportError, match="no ASSESSMENT:"):
            generate_post_mortem(make_spec(), ((make_result(),),), (None,), "failed", llm)
