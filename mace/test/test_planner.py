"""Tier-0 tests for mace.planner.

Run:
    pytest mace/test/test_planner.py -q
"""

from __future__ import annotations

import pytest

from mace.planner import PlanningError, build_prompt, plan
from mace.spec import MaceSpec, Task
from mace.test.conftest import FakeLLM

PLANNER_TRANSCRIPT = """\
Looking at the objective, I'll break this into three tasks.

TASK: cfg1 | deps= | kind=config | x_tiles=2,y_tiles=2,network_config=2dmesh_config
TASK: run_hello | deps=cfg1 | kind=workload | hello_world.c
TASK: run_accu | deps=cfg1 | kind=workload | accu_test.c
"""


def make_spec(**override):
    kwargs = {
        "workloads": ("hello_world.c", "accu_test.c"),
        "objective": "bring up a 2x2 mesh",
        "target_mesh": (2, 2),
    }
    kwargs.update(override)
    return MaceSpec(**kwargs)


class TestBuildPrompt:
    def test_includes_the_objective_and_mesh_and_workloads(self):
        prompt = build_prompt(make_spec())
        assert "bring up a 2x2 mesh" in prompt
        assert "2x2 tiles" in prompt
        assert "hello_world.c, accu_test.c" in prompt
        assert "ariane" in prompt

    def test_documents_the_caches_line_format(self):
        prompt = build_prompt(make_spec())
        assert "CACHES:" in prompt
        assert "l1d" in prompt


class TestPlan:
    def test_returns_the_parsed_task_dag(self):
        llm = FakeLLM(responses=[PLANNER_TRANSCRIPT])
        tasks = plan(make_spec(), llm)
        assert [t.id for t in tasks] == ["cfg1", "run_hello", "run_accu"]
        assert tasks[1].deps == ("cfg1",)

    def test_dispatches_with_the_built_prompt_and_tools(self):
        llm = FakeLLM(responses=[PLANNER_TRANSCRIPT])
        sentinel_tool = object()

        plan(make_spec(), llm, tools=[sentinel_tool])

        assert len(llm.calls) == 1
        message, tools = llm.calls[0]
        assert message == build_prompt(make_spec())
        assert tools == (sentinel_tool,)

    def test_no_feedback_by_default(self):
        assert "Feedback from a previous attempt" not in build_prompt(make_spec())

    def test_feedback_is_appended_when_given(self):
        prompt = build_prompt(make_spec(), feedback="task x failed: timeout")
        assert "Feedback from a previous attempt" in prompt
        assert "task x failed: timeout" in prompt

    def test_feedback_is_passed_through_to_the_prompt(self):
        llm = FakeLLM(responses=[PLANNER_TRANSCRIPT])
        plan(make_spec(), llm, feedback="task x failed: timeout")
        message, _ = llm.calls[0]
        assert message == build_prompt(make_spec(), feedback="task x failed: timeout")

    def test_no_task_lines_raises_planning_error(self):
        llm = FakeLLM(responses=["I don't have enough information to plan yet."])
        with pytest.raises(PlanningError, match="no TASK:"):
            plan(make_spec(), llm)

    def test_cycle_raises_planning_error(self):
        llm = FakeLLM(
            responses=[
                "TASK: a | deps=b | kind=config | x\n"
                "TASK: b | deps=a | kind=config | y\n"
            ]
        )
        with pytest.raises(PlanningError, match="invalid task DAG"):
            plan(make_spec(), llm)

    def test_unknown_dep_raises_planning_error(self):
        llm = FakeLLM(responses=["TASK: a | deps=nope | kind=config | x\n"])
        with pytest.raises(PlanningError, match="invalid task DAG"):
            plan(make_spec(), llm)
