"""Tier-0 tests for config narrowing inside mace.orchestrator.run_mace_loop.

Run:
    pytest mace/test/test_orchestrator_narrowing.py -q

The integrator is a fake that builds nothing: a task passes when
``passes(config)`` says so, for the plan's own tasks and for the probes
narrowing sends through the same call. These tests check what the loop does
with the answer: skip triage on a definitive one, give triage the note on an
inconclusive one, feed the plan check, and record the probes.
"""

from __future__ import annotations

import json

import pytest

from mace.loop import _config_for_task
from mace.orchestrator import run_mace_loop
from mace.spec import Budget, LoopOptions, Task, Triage
from mace.test.conftest import FakeLLM
from mace.test.test_orchestrator import _no_post_mortem, make_db, make_spec, step_result
from mace.test.test_orchestrator_plan_check import scripted_planner

FPGA = "PITON_FPGA_SYNTH"


class FakeNode:
    def close(self):
        pass


@pytest.fixture(autouse=True)
def _two_fake_nodes(monkeypatch):
    monkeypatch.setattr("mace.orchestrator.open_nodes", lambda piton_roots: [FakeNode(), FakeNode()])


def base_task(id="t1", **kwargs):
    return Task(id=id, deps=(), kind="workload", spec="hello_world.c", **kwargs)


def built(task, spec, passes):
    """The result of building *task* under *spec*: it passes when *passes(config)* says so."""
    config = _config_for_task(spec, task)
    ok = passes(config)
    result = step_result(task.id, passed=ok, verdict="pass" if ok else "timeout")
    result.build.config = config
    result.task = task
    return result


def run(tmp_path, monkeypatch, plans, passes, triage=None, options=None, max_iterations=3):
    spec = make_spec(budget=Budget(max_iterations=max_iterations))
    seen, calls, triage_calls = [], [], []
    monkeypatch.setattr("mace.orchestrator.plan", scripted_planner(plans, seen))

    def integrate(piton_roots, spec_, tasks, llm, tools=(), run_id=None, iteration=0, nodes=None, options=None,
                  deadline=None, **kwargs):
        calls.append({"tasks": tasks, "nodes": nodes, "options": options, "deadline": deadline, "iteration": iteration})
        return tuple(built(t, spec_, passes) for t in tasks)

    def fake_triage(result, llm, tools=(), **kwargs):
        triage_calls.append(kwargs)
        if triage is None:
            raise AssertionError("triage must not run")
        return triage

    monkeypatch.setattr("mace.orchestrator.integrate_parallel", integrate)
    monkeypatch.setattr("mace.orchestrator.triage", fake_triage)
    monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)
    db = make_db(tmp_path)
    result = run_mace_loop(("/fake/root",), spec, FakeLLM(responses=[]), db=db, options=options)
    return result, db, seen, calls, triage_calls


def no_fpga(config):
    return FPGA not in config.config_rtl


class TestADefiniteAnswer:
    def test_the_culprit_skips_triage_and_goes_to_the_next_plan_as_the_diagnosis(self, tmp_path, monkeypatch):
        result, db, seen, calls, _ = run(
            tmp_path, monkeypatch, [(base_task(config_rtl=(FPGA,)),), (base_task("t2"),)], no_fpga
        )
        assert result.status == "passed"
        failure = db.query_one("SELECT diagnosis, fix FROM failures WHERE run_id = ?", (result.run_id,))
        assert (failure["diagnosis"], failure["fix"]) == ("config_change", f"leave out the RTL define {FPGA}")
        feedback = seen[1]["feedback"]
        assert "Diagnosis from the probes above: diagnosis=config_change" in feedback
        assert f"without the RTL define {FPGA}: passed" in feedback
        assert "LLM" not in feedback

    def test_the_probe_runs_through_the_integrator_on_the_runs_nodes_without_task_prompts(self, tmp_path, monkeypatch):
        _, _, _, calls, _ = run(tmp_path, monkeypatch, [(base_task(config_rtl=(FPGA,)),), (base_task("t2"),)], no_fpga)
        probe = calls[1]
        assert [t.id for t in probe["tasks"]] == ["narrow-t1-0"]
        assert len(probe["nodes"]) == 2 and probe["deadline"] is not None and probe["iteration"] == 0
        assert probe["options"].task_prompts is False and probe["options"].rtl_edits is False
        assert FPGA not in _config_for_task(make_spec(), probe["tasks"][0]).config_rtl

    def test_the_probes_are_recorded_apart_from_the_plans_tasks(self, tmp_path, monkeypatch):
        result, db, _, _, _ = run(tmp_path, monkeypatch, [(base_task(config_rtl=(FPGA,)),), (base_task("t2"),)], no_fpga)
        rows = db.query("SELECT * FROM probes WHERE run_id = ?", (result.run_id,))
        assert [(r["failed_task"], r["task_id"], r["verdict"], r["ran"], r["passed"]) for r in rows] == [
            ("t1", "narrow-t1-0", "culprit", 1, 1)
        ]
        assert json.loads(rows[0]["undone"]) == [f"without the RTL define {FPGA}"]
        tasks = db.query("SELECT task_id FROM tasks WHERE run_id = ?", (result.run_id,))
        assert not any(t["task_id"].startswith("narrow-") for t in tasks)

    def test_a_replan_that_keeps_the_culprit_beside_another_change_is_sent_back(self, tmp_path, monkeypatch):
        keeps = (base_task("t2", config_rtl=(FPGA,), caches=(("l2", (32768, 4)),)),)
        result, db, seen, calls, _ = run(
            tmp_path, monkeypatch, [(base_task(config_rtl=(FPGA,)),), keeps, (base_task("t3"),)], no_fpga
        )
        assert [s["phase"] for s in seen] == ["plan", "plan", "plan_retry"]
        assert f"leave out the RTL define {FPGA}" in seen[2]["rejection"]
        rows = db.query("SELECT iteration, attempt, action, problems FROM plan_checks WHERE run_id = ? ORDER BY attempt",
                        (result.run_id,))
        assert [(r["iteration"], r["action"], json.loads(r["problems"])[0][0] if json.loads(r["problems"]) else None)
                for r in rows] == [(1, "rejected", "known_bad_change"), (1, "accepted", None)]
        assert result.status == "passed"


class TestAnInconclusiveAnswer:
    DEFAULTS_FAIL = staticmethod(lambda config: False)

    def test_triage_runs_with_the_note_and_the_next_plan_sees_it(self, tmp_path, monkeypatch):
        verdict = Triage(diagnosis="rtl_suspect", fix="look at manycore_top.v")
        result, db, seen, calls, triage_calls = run(
            tmp_path, monkeypatch, [(base_task(config_rtl=(FPGA,)),), (base_task("t2", caches=(("l2", (32768, 4)),)),)],
            lambda config: config.caches["l2"] == (32768, 4), triage=verdict,
        )
        assert "default configuration fails the same way" in triage_calls[0]["narrowing"]
        feedback = seen[1]["feedback"]
        assert "default configuration fails the same way" in feedback
        assert "Triage, an LLM's reading of this evidence: diagnosis=rtl_suspect" in feedback
        failure = db.query_one("SELECT diagnosis FROM failures WHERE run_id = ?", (result.run_id,))
        assert failure["diagnosis"] == "rtl_suspect"

    def test_a_probe_that_failed_is_a_failed_configuration_a_replan_may_not_repeat(self, tmp_path, monkeypatch):
        verdict = Triage(diagnosis="rtl_suspect", fix="x")
        # The probe builds the defaults and fails; a re-plan of the defaults is the same build.
        result, db, seen, calls, _ = run(
            tmp_path, monkeypatch,
            [(base_task(config_rtl=(FPGA,)),), (base_task("t2"),), (base_task("t3", caches=(("l2", (32768, 4)),)),)],
            lambda config: config.caches["l2"] == (32768, 4), triage=verdict,
        )
        assert [s["phase"] for s in seen] == ["plan", "plan", "plan_retry"]
        assert "same configuration as task narrow-t1-0 in iteration 1" in seen[2]["rejection"]


class TestWhenNarrowingDoesNotRun:
    def test_it_can_be_turned_off(self, tmp_path, monkeypatch):
        verdict = Triage(diagnosis="rtl_suspect", fix="x")
        result, db, seen, calls, triage_calls = run(
            tmp_path, monkeypatch, [(base_task(config_rtl=(FPGA,)),), (base_task("t2"),)], no_fpga,
            triage=verdict, options=LoopOptions(narrowing=False),
        )
        assert [len(c["tasks"]) for c in calls] == [1, 1] and not any(
            t.id.startswith("narrow-") for c in calls for t in c["tasks"]
        )
        assert triage_calls[0]["narrowing"] == "" and db.query("SELECT * FROM probes") == []

    def test_a_failure_with_no_change_from_the_defaults_is_not_probed(self, tmp_path, monkeypatch):
        verdict = Triage(diagnosis="rtl_suspect", fix="x")
        _, db, _, calls, triage_calls = run(
            tmp_path, monkeypatch, [(base_task(),), (base_task("t2", caches=(("l2", (32768, 4)),)),)],
            lambda config: config.caches["l2"] == (32768, 4), triage=verdict,
        )
        assert len(calls) == 2 and db.query("SELECT * FROM probes") == []
        assert triage_calls[0]["narrowing"] == ""

    def test_the_raw_triage_mode_does_not_probe(self, tmp_path, monkeypatch):
        _, db, _, calls, _ = run(
            tmp_path, monkeypatch, [(base_task(config_rtl=(FPGA,)),), (base_task("t2"),)], no_fpga,
            options=LoopOptions(triage="raw"),
        )
        assert len(calls) == 2 and db.query("SELECT * FROM probes") == []
