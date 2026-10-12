"""Tier-0 tests for mace.fast_path, and for the loop's use of it.

Run:
    pytest mace/test/test_fast_path.py -q
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from mace.fast_path import collapse_single_design
from mace.orchestrator import run_mace_loop
from mace.spec import Budget, LoopOptions, MaceSpec, Task, Triage
from mace.test.conftest import FakeLLM
from mace.test.test_orchestrator import _no_post_mortem, make_db, make_spec, step_result
from mace.test.test_orchestrator_plan_check import scripted_planner

BIST = "CONFIG_DISABLE_BIST_CLEAR"


def spec():
    return MaceSpec(workloads=("hello_world.c",), objective="o", core="ariane", target_mesh=(2, 2))


def task(id, kind="workload", deps=(), **kwargs):
    return Task(id=id, deps=deps, kind=kind, spec=f"instruction of {id}", **kwargs)


class TestCollapseSingleDesign:
    def test_one_task_is_a_single_design_with_nothing_dropped(self):
        kept, dropped = collapse_single_design(spec(), (task("a"),))
        assert [t.id for t in kept] == ["a"] and dropped == ()

    def test_tasks_that_build_the_same_flags_collapse_to_the_last_one(self):
        # The recorded shape: a config task, then workload tasks that depend on it and reuse its build.
        plan = (
            task("cfg", kind="config", config_rtl=(BIST,)),
            task("run1", deps=("cfg",), config_rtl=(BIST,)),
            task("run2", deps=("run1",), config_rtl=(BIST,)),
        )
        kept, dropped = collapse_single_design(spec(), plan)
        assert [t.id for t in kept] == ["run2"] and kept[0].deps == () and dropped == ("cfg", "run1")
        assert kept[0].config_rtl == (BIST,) and kept[0].spec == "instruction of run2"

    def test_the_kept_task_is_the_last_in_dependency_order_not_the_last_listed(self):
        plan = (task("run", deps=("cfg",)), task("cfg", kind="config"))
        kept, dropped = collapse_single_design(spec(), plan)
        assert kept[0].id == "run" and dropped == ("cfg",)

    def test_tasks_that_differ_only_in_their_instruction_are_one_design(self):
        plan = (task("a"), task("b"), task("c"))
        assert collapse_single_design(spec(), plan)[1] == ("a", "b")

    @pytest.mark.parametrize("second", [
        task("b", config_rtl=(BIST,)),
        task("b", caches=(("l2", (32768, 4)),)),
        task("b", network="xbar_config"),
        Task(id="b", deps=(), kind="config", spec="s", drop_rtl=("MINIMAL_MONITORING",)),
    ])
    def test_tasks_that_build_different_flags_are_not_collapsed(self, second):
        assert collapse_single_design(spec(), (task("a"), second)) is None

    @pytest.mark.parametrize("kind", ["unit_test", "rtl"])
    def test_a_plan_with_another_kind_of_task_is_not_collapsed(self, kind):
        assert collapse_single_design(spec(), (task("a"), task("b", kind=kind))) is None

    def test_an_empty_plan_is_not_collapsed(self):
        assert collapse_single_design(spec(), ()) is None


@pytest.fixture(autouse=True)
def _no_real_nodes(monkeypatch):
    monkeypatch.setattr("mace.orchestrator.open_nodes", lambda piton_roots: [])


def run(tmp_path, monkeypatch, plans, options=None, passes=(True,), hook=None):
    seen, calls = [], []
    monkeypatch.setattr("mace.orchestrator.plan", scripted_planner(plans, seen))

    def integrate(piton_roots, spec_, tasks, llm, tools=(), run_id=None, iteration=0, options=None, **kwargs):
        calls.append({"tasks": tasks, "options": options})
        ok = passes[iteration]
        return tuple(step_result(t.id, passed=ok, verdict="pass" if ok else "timeout") for t in tasks)

    monkeypatch.setattr("mace.orchestrator.integrate_parallel", integrate)
    monkeypatch.setattr("mace.orchestrator.triage", lambda *a, **k: Triage("config_error", "x"))
    monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)
    db = make_db(tmp_path)
    result = run_mace_loop(
        ("/fake/root",), make_spec(budget=Budget(max_iterations=len(passes))), FakeLLM(responses=[]), db=db,
        options=options, plan_hook=hook,
    )
    return result, db, calls


class TestInTheLoop:
    def one_design(self):
        return (
            Task(id="cfg", deps=(), kind="config", spec="configure"),
            Task(id="run", deps=("cfg",), kind="workload", spec="run it"),
        )

    def test_a_single_design_runs_as_one_task_with_no_task_calls(self, tmp_path, monkeypatch):
        result, db, calls = run(tmp_path, monkeypatch, [self.one_design()])
        assert [t.id for t in calls[0]["tasks"]] == ["run"] and calls[0]["options"].task_prompts is False
        row = db.query_one("SELECT kept, dropped FROM fast_paths WHERE run_id = ?", (result.run_id,))
        assert (row["kept"], json.loads(row["dropped"])) == ("run", ["cfg"])

    def test_a_plan_of_two_designs_keeps_its_tasks_but_makes_no_task_calls(self, tmp_path, monkeypatch):
        plan = (
            Task(id="a", deps=(), kind="workload", spec="default"),
            Task(id="b", deps=(), kind="workload", spec="small l2", caches=(("l2", (32768, 4)),)),
        )
        result, db, calls = run(tmp_path, monkeypatch, [plan])
        assert [t.id for t in calls[0]["tasks"]] == ["a", "b"] and calls[0]["options"].task_prompts is False
        assert db.query("SELECT * FROM fast_paths") == []  # nothing collapsed

    def test_without_the_fast_path_a_plan_of_two_designs_keeps_its_task_calls(self, tmp_path, monkeypatch):
        plan = (
            Task(id="a", deps=(), kind="workload", spec="default"),
            Task(id="b", deps=(), kind="workload", spec="small l2", caches=(("l2", (32768, 4)),)),
        )
        _, _, calls = run(tmp_path, monkeypatch, [plan], options=LoopOptions(fast_path=False))
        assert [t.id for t in calls[0]["tasks"]] == ["a", "b"] and calls[0]["options"].task_prompts is True

    def test_an_rtl_task_still_gets_its_agent_call_when_task_calls_are_off(self):
        # integrate's rtl branch runs on kind == "rtl" and options.rtl_edits, whatever task_prompts says.
        import inspect

        from mace import integrator

        source = inspect.getsource(integrator._run_batch)
        assert 'rtl = task.kind == "rtl" and options.rtl_edits' in source
        assert source.index("if rtl:") < source.index("elif options.task_prompts:")

    def test_it_can_be_turned_off(self, tmp_path, monkeypatch):
        _, db, calls = run(tmp_path, monkeypatch, [self.one_design()], options=LoopOptions(fast_path=False))
        assert [t.id for t in calls[0]["tasks"]] == ["cfg", "run"] and calls[0]["options"].task_prompts is True
        assert db.query("SELECT * FROM fast_paths") == []

    def test_a_task_prompts_off_run_stays_off(self, tmp_path, monkeypatch):
        _, _, calls = run(tmp_path, monkeypatch, [self.one_design()], options=LoopOptions(task_prompts=False))
        assert calls[0]["options"].task_prompts is False

    def test_a_seeded_fault_is_applied_before_the_plan_collapses(self, tmp_path, monkeypatch):
        def hook(iteration, tasks):
            return tuple(dataclasses.replace(t, config_rtl=("PITON_FPGA_SYNTH",)) for t in tasks)

        _, _, calls = run(tmp_path, monkeypatch, [self.one_design()], hook=hook)
        assert [t.id for t in calls[0]["tasks"]] == ["run"] and calls[0]["tasks"][0].config_rtl == ("PITON_FPGA_SYNTH",)

    def test_each_iteration_is_collapsed_on_its_own(self, tmp_path, monkeypatch):
        replan = (Task(id="only", deps=(), kind="workload", spec="again", caches=(("l2", (32768, 4)),)),)
        result, db, calls = run(tmp_path, monkeypatch, [self.one_design(), replan], passes=(False, True))
        assert [c["tasks"][0].id for c in calls] == ["run", "only"]
        rows = db.query("SELECT iteration, kept, dropped FROM fast_paths WHERE run_id = ? ORDER BY iteration", (result.run_id,))
        assert [(r["iteration"], r["kept"], json.loads(r["dropped"])) for r in rows] == [(0, "run", ["cfg"]), (1, "only", [])]
