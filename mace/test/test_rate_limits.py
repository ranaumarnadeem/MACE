"""Tier-0 tests: the planner, triage, the post-mortem, and the retry agent
wait and try again when the backend rate-limits a call (Vertex's HTTP 429),
through mace.usage.prompt_retrying. A rate limit that outlasts the waits
still ends the run as an error, which a batch runs again.

Run:
    pytest mace/test/test_rate_limits.py -q
"""

from __future__ import annotations

import pytest
from chia.base.llm_call import QueryResult

from mace import metrics
from mace.baselines.retry_agent import run_retry_agent
from mace.orchestrator import run_mace_loop
from mace.planner import plan
from mace.report import generate_post_mortem
from mace.spec import Budget, MaceSpec
from mace.test.test_orchestrator import step_result
from mace.triage import triage

SPEC = MaceSpec(workloads=("addi.S",), objective="Verify addi.S.", core="pico", budget=Budget(max_iterations=1))


class Limited(Exception):
    """What a backend raises on HTTP 429."""

    error_type = "rate_limit"


class Llm:
    """Raises *errors* in turn, then answers every call with *reply*."""

    def __init__(self, errors, reply):
        self.errors = list(errors)
        self.reply = reply
        self.calls = 0

    def prompt(self, message, tools=()):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return QueryResult(result=self.reply, returncode=0, stderr="", stream_result=self.reply, success=True)


@pytest.fixture
def waits(monkeypatch):
    waits = []
    monkeypatch.setattr("mace.usage.time.sleep", lambda seconds: waits.append(seconds))
    return waits


def test_triage_waits_and_tries_again(waits):
    llm = Llm([Limited("429")], "DIAGNOSIS: timeout\nFIX: raise rtl_timeout\n")
    diagnosis = triage(step_result(passed=False, verdict="timeout"), llm)
    assert diagnosis.diagnosis == "timeout" and llm.calls == 2 and waits == [30]


def test_triage_raises_once_the_waits_run_out(waits):
    llm = Llm([Limited("429")] * 9, "DIAGNOSIS: timeout\nFIX: x\n")
    with pytest.raises(Limited):
        triage(step_result(passed=False, verdict="timeout"), llm)
    assert waits == [30, 60, 120]


def test_the_planner_waits_and_tries_again(waits):
    llm = Llm([Limited("429"), Limited("429")], "TASK: t1 | deps= | kind=workload | addi.S")
    (task,) = plan(SPEC, llm)
    assert task.id == "t1" and waits == [30, 60]


def test_the_post_mortem_waits_and_tries_again(waits):
    llm = Llm([Limited("429")], "ASSESSMENT: inconclusive\n")
    report = generate_post_mortem(SPEC, (), (), "budget_exceeded", llm)
    assert report.assessment == "inconclusive" and waits == [30]


def test_the_retry_agent_waits_and_tries_again(tmp_path, waits):
    db = metrics.open_db(str(tmp_path / "m.db"), ray_placement=False)
    llm = Llm([Limited("429")], "no design here")
    result = run_retry_agent("/fake/root", SPEC, llm, db)
    assert result.status == "budget_exceeded" and waits == [30]


def test_a_rate_limit_that_outlasts_the_waits_ends_the_run_as_an_error(tmp_path, waits):
    db = metrics.open_db(str(tmp_path / "m.db"), ray_placement=False)
    with pytest.raises(Limited):
        run_mace_loop(("/fake/root",), SPEC, Llm([Limited("429")] * 9, ""), db=db)
    assert [r["status"] for r in db.query("SELECT status FROM runs")] == ["error"]
