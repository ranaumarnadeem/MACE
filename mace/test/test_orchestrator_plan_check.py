"""Tier-0 tests for the plan check inside mace.orchestrator.run_mace_loop.

Run:
    pytest mace/test/test_orchestrator_plan_check.py -q

Like test_orchestrator.py, these replace the planner and the integrator with
plain fakes, so they test what the loop does with a plan before it runs:
reject it, send it back, clean it, or run it anyway.
"""

from __future__ import annotations

import json

import pytest

from mace import plan_check
from mace.orchestrator import MAX_PLAN_REJECTIONS, run_mace_loop
from mace.planner import PlanningError
from mace.spec import Budget, LoopOptions, Task, Triage
from mace.test.conftest import FakeLLM
from mace.test.test_orchestrator import _no_post_mortem, make_db, make_spec, step_result

BIST = "CONFIG_DISABLE_BIST_CLEAR"
NEEDS_BIST = f"Verify hello_world.c. That task needs the {BIST} RTL define."


@pytest.fixture(autouse=True)
def _no_real_nodes(monkeypatch):
    monkeypatch.setattr("mace.orchestrator.open_nodes", lambda piton_roots: [])


def base_task(id="t1", **kwargs):
    return Task(id=id, deps=(), kind="workload", spec="hello_world.c", **kwargs)


def scripted_planner(replies, seen):
    """A planner stand-in: the next of *replies* per call (an exception is raised), and each call's
    feedback, rejection note, and phase appended to *seen*."""
    queue = list(replies)

    def _plan(spec, llm, tools=(), feedback="", rtl_edits=False, rejection="", phase="plan"):
        seen.append({"feedback": feedback, "rejection": rejection, "phase": phase})
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return _plan


def check_rows(db, run_id):
    """(iteration, attempt, action, rules, stripped) of each plan_checks row of the run."""
    return [
        (r["iteration"], r["attempt"], r["action"], [p[0] for p in json.loads(r["problems"])], json.loads(r["stripped"]))
        for r in db.query("SELECT * FROM plan_checks WHERE run_id = ? ORDER BY iteration, attempt", (run_id,))
    ]


def run(tmp_path, monkeypatch, replies, spec=None, passes=None, options=None, roots=("/fake/root",), hook=None):
    """run_mace_loop with scripted plans. *passes* gives each iteration's outcome (default: pass)."""
    seen, ran = [], []
    monkeypatch.setattr("mace.orchestrator.plan", scripted_planner(replies, seen))

    def integrate(piton_roots, spec_, tasks, llm, tools=(), run_id=None, iteration=0, **kwargs):
        ran.append(tasks)
        passed = passes[iteration] if passes else True
        return (step_result("t1", passed=passed, verdict="pass" if passed else "fail"),)

    monkeypatch.setattr("mace.orchestrator.integrate_parallel", integrate)
    monkeypatch.setattr("mace.orchestrator.triage", lambda *a, **k: Triage(diagnosis="config_error", fix="x"))
    monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)
    db = make_db(tmp_path)
    result = run_mace_loop(
        roots, spec or make_spec(objective=NEEDS_BIST, budget=Budget(max_iterations=3)), FakeLLM(responses=[]),
        db=db, options=options, plan_hook=hook,
    )
    return result, db, seen, ran


class TestRejection:
    def test_a_plan_that_cannot_work_goes_back_with_its_reasons(self, tmp_path, monkeypatch):
        bad, good = (base_task(),), (base_task(config_rtl=(BIST,)),)
        result, db, seen, ran = run(tmp_path, monkeypatch, [bad, good])
        assert [s["phase"] for s in seen] == ["plan", "plan_retry"]
        assert seen[0]["rejection"] == ""
        assert "rejected before any build" in seen[1]["rejection"]
        assert BIST in seen[1]["rejection"] and "task t1" in seen[1]["rejection"]
        assert ran == [good] and result.status == "passed"
        assert check_rows(db, result.run_id) == [
            (0, 0, "rejected", ["required_define"], []),
            (0, 1, "accepted", [], []),
        ]

    def test_the_failures_feedback_goes_to_the_retry_as_well(self, tmp_path, monkeypatch):
        bad, good = (base_task(),), (base_task(config_rtl=(BIST,)),)
        _, _, seen, _ = run(tmp_path, monkeypatch, [good, bad, good], passes=[False, True])
        assert seen[1]["feedback"] != "" and seen[1]["feedback"] == seen[2]["feedback"]

    def test_a_plan_rejected_every_time_runs_as_it_stands_after_the_cap(self, tmp_path, monkeypatch):
        bad = (base_task(),)
        result, db, seen, ran = run(tmp_path, monkeypatch, [bad] * (MAX_PLAN_REJECTIONS + 1))
        assert len(seen) == MAX_PLAN_REJECTIONS + 1 and ran == [bad]
        rows = check_rows(db, result.run_id)
        assert [row[2] for row in rows] == ["rejected"] * MAX_PLAN_REJECTIONS + ["accepted"]
        assert rows[-1][3] == ["required_define"]

    def test_a_retry_with_no_usable_plan_leaves_the_rejected_plan_in_force(self, tmp_path, monkeypatch):
        bad = (base_task(),)
        result, db, _, ran = run(tmp_path, monkeypatch, [bad, PlanningError("no TASK: lines")])
        assert ran == [bad] and result.status == "passed"
        assert [row[2] for row in check_rows(db, result.run_id)] == ["rejected", "kept"]

    def test_a_clean_plan_makes_one_planner_call_and_leaves_no_record(self, tmp_path, monkeypatch):
        result, db, seen, _ = run(tmp_path, monkeypatch, [(base_task(config_rtl=(BIST,)),)])
        assert len(seen) == 1 and check_rows(db, result.run_id) == []

    def test_the_check_can_be_turned_off(self, tmp_path, monkeypatch):
        bad = (base_task(),)
        result, db, seen, ran = run(tmp_path, monkeypatch, [bad], options=LoopOptions(plan_check=False))
        assert len(seen) == 1 and ran == [bad] and check_rows(db, result.run_id) == []


class TestSeededFaults:
    def test_a_fault_breaks_the_plan_after_the_check_not_before(self, tmp_path, monkeypatch):
        good = (base_task(config_rtl=(BIST,)),)
        broken = (base_task(config_rtl=(BIST, "PITON_FPGA_SYNTH")),)
        _, _, seen, ran = run(tmp_path, monkeypatch, [good], hook=lambda iteration, tasks: broken)
        assert len(seen) == 1 and ran == [broken]


class TestRepeatedFailure:
    def test_a_replan_that_rebuilds_a_failed_configuration_is_sent_back(self, tmp_path, monkeypatch):
        spec = make_spec(budget=Budget(max_iterations=3))  # no required define
        default = (base_task(),)
        other = (base_task("t2", caches=(("l2", (32768, 4)),)),)
        result, db, seen, ran = run(
            tmp_path, monkeypatch, [default, default, other], spec=spec, passes=[False, True]
        )
        assert [s["phase"] for s in seen] == ["plan", "plan", "plan_retry"]
        assert "same configuration as task t1 in iteration 1" in seen[2]["rejection"]
        assert ran == [default, other] and result.status == "passed"
        assert check_rows(db, result.run_id) == [
            (1, 0, "rejected", ["repeated_failure"], []),
            (1, 1, "accepted", [], []),
        ]

    def test_a_different_configuration_is_not_a_repeat(self, tmp_path, monkeypatch):
        spec = make_spec(budget=Budget(max_iterations=3))
        other = (base_task("t2", caches=(("l2", (32768, 4)),)),)
        _, _, seen, _ = run(tmp_path, monkeypatch, [(base_task(),), other], spec=spec, passes=[False, True])
        assert [s["phase"] for s in seen] == ["plan", "plan"]


class TestUnknownDefines:
    def make_checkout(self, tmp_path):
        checkout = tmp_path / "checkout"
        (checkout / "piton" / "design").mkdir(parents=True)
        (checkout / "piton" / "design" / "x.v").write_text(f"`ifdef {BIST}\n`endif\n")
        plan_check.define_index.cache_clear()
        return str(checkout)

    def test_a_define_no_file_mentions_is_removed_before_the_plan_runs(self, tmp_path, monkeypatch):
        invented = (base_task(config_rtl=(BIST, "MADE_UP_FLAG")),)
        result, db, seen, ran = run(tmp_path, monkeypatch, [invented], roots=(self.make_checkout(tmp_path),))
        assert ran[0][0].config_rtl == (BIST,) and len(seen) == 1
        assert check_rows(db, result.run_id) == [(0, 0, "accepted", [], ["MADE_UP_FLAG"])]

    def test_a_checkout_that_is_not_a_directory_changes_nothing(self, tmp_path, monkeypatch):
        invented = (base_task(config_rtl=(BIST, "MADE_UP_FLAG")),)
        _, _, _, ran = run(tmp_path, monkeypatch, [invented])
        assert ran[0][0].config_rtl == (BIST, "MADE_UP_FLAG")
