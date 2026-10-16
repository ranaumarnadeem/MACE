"""Tier-0 tests for two parts of mace.orchestrator.run_mace_loop that keep a
run's progress: a run passes once one task ran every gate program to a pass
(LoopOptions.gate_pass_ends_run), and the RTL edits of the last task that
built carry into the next iteration (LoopOptions.carry_edits).

Run:
    pytest mace/test/test_loop_progress.py -q
"""

from __future__ import annotations

import dataclasses

import pytest

from chia_openpiton.state_def import PitonRunResult
from mace import metrics
from mace.integrator import BASE_EDITS, inherited_edits
from mace.orchestrator import gate_passed, run_mace_loop
from mace.spec import Budget, LoopOptions, MaceSpec, Task
from mace.test.conftest import FakeLLM
from mace.test.test_orchestrator import step_result

OPTIONS = LoopOptions(triage="off", narrowing=False, post_mortem=False, plan_check=False)
SPEC = MaceSpec(workloads=("addi.S", "lw.S"), objective="Verify addi.S and lw.S.", core="pico",
                budget=Budget(max_iterations=2))


def run(test, success=True):
    return PitonRunResult(success=success, returncode=0, test=test, sim_type="vlt", run_dir="/x/r",
                          verdict="pass" if success else "fail")


def result(task_id, passed, programs=("addi.S", "lw.S"), kind="workload", edits=None, built=True):
    r = step_result(task_id, passed=passed, build_success=built)
    runs = tuple(run(p, passed) for p in programs) if built else ()
    return dataclasses.replace(r, task=Task(id=task_id, deps=(), kind=kind, spec="x"), runs=runs, edits=edits)


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """run_mace_loop with a scripted plan and scripted iteration results.
    Records each iteration's base_edits and each plan's feedback."""
    seen = {"base_edits": [], "feedback": []}

    def start(results_by_iteration, options=OPTIONS):
        def plan(spec, llm, tools=(), feedback="", rtl_edits=True, **kwargs):
            seen["feedback"].append(feedback)
            return (Task(id="t1", deps=(), kind="workload", spec="addi.S"),
                    Task(id="t2", deps=(), kind="workload", spec="lw.S"))

        def integrate(piton_roots, spec, tasks, llm, iteration=0, base_edits=None, **kwargs):
            seen["base_edits"].append(dict(base_edits or {}))
            return results_by_iteration[iteration]

        monkeypatch.setattr("mace.orchestrator.plan", plan)
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", integrate)
        monkeypatch.setattr("mace.orchestrator.open_nodes", lambda roots: [])
        options = dataclasses.replace(options, fast_path=False)
        db = metrics.open_db(str(tmp_path / "m.db"), ray_placement=False)
        return run_mace_loop(("/fake/root",), SPEC, FakeLLM(responses=[]), db=db, options=options)

    return start, seen


class TestGatePassed:
    def test_a_task_that_ran_every_gate_program_to_a_pass(self):
        assert gate_passed(SPEC, OPTIONS, result("t1", True))

    def test_not_when_a_gate_program_did_not_run(self):
        assert not gate_passed(SPEC, OPTIONS, result("t1", True, programs=("addi.S",)))

    def test_not_for_a_failed_task_a_test_task_or_a_build_only_check(self):
        assert not gate_passed(SPEC, OPTIONS, result("t1", False))
        assert not gate_passed(SPEC, OPTIONS, result("t1", True, kind="test"))
        assert not gate_passed(SPEC, dataclasses.replace(OPTIONS, check="build"), result("t1", True))


class TestGatePassEndsRun:
    def test_one_passing_task_passes_the_run(self, loop):
        start, seen = loop
        out = start([(result("t1", True), result("t2", False))])
        assert out.status == "passed" and len(out.iterations) == 1

    def test_switched_off_the_failed_task_fails_the_iteration(self, loop):
        start, _ = loop
        failing = (result("t1", True), result("t2", False))
        out = start([failing, failing], options=dataclasses.replace(OPTIONS, gate_pass_ends_run=False))
        assert out.status == "budget_exceeded" and len(out.iterations) == 2


class TestCarryEdits:
    EDITS = {"piton/design/chip/tile/serv/generic_core_wrapper.v": "module generic_core_wrapper; endmodule\n"}

    def test_the_last_edits_that_built_carry_into_the_next_iteration(self, loop):
        start, seen = loop
        first = (result("t1", False, edits=self.EDITS), result("t2", False, built=False, edits={"x": "y"}))
        out = start([first, (result("t1", False), result("t2", False))])
        assert out.status == "budget_exceeded"
        assert seen["base_edits"] == [{}, self.EDITS]
        assert "stay applied for every task of the next plan" in seen["feedback"][1]

    def test_edits_whose_build_failed_do_not_carry(self, loop):
        start, seen = loop
        failed_build = (result("t1", False, built=False, edits=self.EDITS), result("t2", False))
        start([failed_build, failed_build])
        assert seen["base_edits"] == [{}, {}]

    def test_switched_off_nothing_carries(self, loop):
        start, seen = loop
        first = (result("t1", False, edits=self.EDITS), result("t2", False))
        start([first, first], options=dataclasses.replace(OPTIONS, carry_edits=False))
        assert seen["base_edits"] == [{}, {}]


def test_every_task_inherits_the_base_edits_under_its_dependencies():
    applied = {BASE_EDITS: {"a": "base", "b": "base"}, "t1": {"b": "t1"}}
    task = Task(id="t2", deps=("t1",), kind="workload", spec="x")
    assert inherited_edits(task, applied) == {"a": "base", "b": "t1"}
    assert inherited_edits(Task(id="t3", deps=(), kind="workload", spec="x"), applied) == {"a": "base", "b": "base"}
