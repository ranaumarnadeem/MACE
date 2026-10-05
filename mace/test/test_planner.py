"""Tier-0 tests for mace.planner.

Run:
    pytest mace/test/test_planner.py -q
"""

from __future__ import annotations

import pytest

from mace.planner import PlanningError, build_prompt, inherit_config_rtl, plan
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

    def test_states_ariane_s_way_rule(self):
        prompt = " ".join(build_prompt(make_spec()).split())
        assert "On Ariane, neither l1d nor l1i may have more ways than l15" in prompt

    def test_documents_unit_test_as_a_kind(self):
        prompt = build_prompt(make_spec())
        assert "kind=config|workload|unit_test" in prompt
        assert "unit_test" in prompt
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


class TestInheritConfigRtl:
    def test_a_task_gets_its_dependency_s_defines(self):
        tasks = (
            Task(id="cfg", deps=(), kind="config", spec="x", config_rtl=("CONFIG_DISABLE_BIST_CLEAR",)),
            Task(id="run", deps=("cfg",), kind="workload", spec="jal.S"),
        )
        assert inherit_config_rtl(tasks)[1].config_rtl == ("CONFIG_DISABLE_BIST_CLEAR",)

    def test_defines_pass_through_a_chain_and_merge_with_the_task_s_own(self):
        tasks = (
            Task(id="c", deps=("b",), kind="workload", spec="z", config_rtl=("FLAG_C",)),
            Task(id="b", deps=("a",), kind="config", spec="y"),
            Task(id="a", deps=(), kind="config", spec="x", config_rtl=("FLAG_A",)),
        )
        assert [t.config_rtl for t in inherit_config_rtl(tasks)] == [("FLAG_A", "FLAG_C"), ("FLAG_A",), ("FLAG_A",)]

    def test_tasks_without_dependencies_and_caches_are_unchanged(self):
        tasks = (
            Task(id="cfg", deps=(), kind="config", spec="x", caches=(("l1d", (16384, 4)),)),
            Task(id="run", deps=("cfg",), kind="workload", spec="y"),
            Task(id="alone", deps=(), kind="workload", spec="z"),
        )
        assert inherit_config_rtl(tasks) == tasks

    def test_plan_applies_it(self):
        llm = FakeLLM(responses=[
            "TASK: cfg | deps= | kind=config | x\n"
            "CONFIG_RTL: cfg | CONFIG_DISABLE_BIST_CLEAR\n"
            "TASK: run | deps=cfg | kind=workload | jal.S\n"
        ])
        assert plan(make_spec(), llm)[1].config_rtl == ("CONFIG_DISABLE_BIST_CLEAR",)


class TestRtlKind:
    def test_the_prompt_offers_the_rtl_kind_by_default(self):
        prompt = build_prompt(make_spec())
        assert "kind=config|workload|unit_test|rtl" in prompt
        assert 'kind "rtl" is a change to the design' in prompt

    def test_without_rtl_edits_the_prompt_leaves_the_kind_out(self):
        prompt = build_prompt(make_spec(), rtl_edits=False)
        assert "kind=config|workload|unit_test |" in prompt
        assert "|rtl" not in prompt and 'kind "rtl"' not in prompt

    def test_plan_passes_the_choice_to_the_prompt(self):
        llm = FakeLLM(responses=[PLANNER_TRANSCRIPT])
        plan(make_spec(), llm, rtl_edits=False)
        message, _ = llm.calls[0]
        assert message == build_prompt(make_spec(), rtl_edits=False)

    def test_an_rtl_task_line_parses(self):
        llm = FakeLLM(responses=["TASK: fix | deps= | kind=rtl | Move inv.vld into p_rtrn_logic\n"])
        (only,) = plan(make_spec(), llm)
        assert only.kind == "rtl"
