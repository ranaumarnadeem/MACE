"""Tier-0 tests for mace.eval.plan_replay.

Run:
    pytest mace/test/test_plan_replay.py -q
"""

from __future__ import annotations

import sqlite3

import pytest

from chia.base.llm_call import QueryResult
from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics
from mace.eval import plan_replay
from mace.plan_check import REPEATED_FAILURE, REQUIRED_DEFINE, SOURCE_EDIT, define_index
from mace.spec import MaceSpec, StepResult, Task
from mace.usage import LLMCall

BIST = "CONFIG_DISABLE_BIST_CLEAR"
OBJECTIVE = f"Verify addi.S on a 2x2 pico mesh. That task needs the {BIST} RTL define."


def step(task_id, passed, config=None, build_ok=True):
    config = config or PitonConfig(core="pico", x_tiles=2, y_tiles=2)
    build = PitonBuildArtifact(
        success=build_ok, returncode=0 if build_ok else 1, config=config, sim_type="vlt", model_dir="/x",
        binary_path="/x/V", wall_time_s=1.0, failure_reason="" if build_ok else "verilog_error",
    )
    run = PitonRunResult(
        success=passed, returncode=0, test="addi.S", sim_type="vlt", run_dir="/x", verdict="pass" if passed else "timeout"
    ) if build_ok else None
    query = QueryResult(result="", returncode=0, stderr="", stream_result="", success=True)
    return StepResult(task=Task(id=task_id, deps=(), kind="config", spec="s"), query=query, build=build, run=run, passed=passed)


def add_run(db, method, plans, outcomes):
    """A run with one plan reply and one iteration outcome per entry of *plans* / *outcomes*."""
    spec = MaceSpec(workloads=("addi.S",), objective=OBJECTIVE, core="pico", target_mesh=(2, 2))
    run_id = metrics.start_run(db, spec, labels=metrics.RunLabels(method=method, task="pico-2x2-addi", repeat=0))
    for i, (reply, results) in enumerate(zip(plans, outcomes)):
        metrics.record_iteration(db, run_id, i, results, wall_s=1.0)
        metrics.record_llm_calls(db, run_id, i, (LLMCall("plan", reply=reply),))
    metrics.finish_run(db, run_id, "passed")
    return run_id


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "eval.db")
    db = metrics.open_db(path, ray_placement=False)
    good = f"TASK: t1 | deps= | kind=config | Configure 2x2 pico\nCONFIG_RTL: t1 | {BIST}\n"
    no_define = "TASK: t1 | deps= | kind=config | Configure 2x2 pico\n"
    edit = f"TASK: t1 | deps= | kind=config | Update manycore_top.v to define async_mux\nCONFIG_RTL: t1 | {BIST}\n"
    invented = f"TASK: t1 | deps= | kind=config | Configure\nCONFIG_RTL: t1 | {BIST} MADE_UP_FLAG\n"
    add_run(db, "mace", [good], [(step("t1", True),)])
    # iteration 0 builds the default and fails; iteration 1 plans the same default again.
    add_run(db, "mace", [no_define, no_define], [(step("t1", False),), (step("t1", False),)])
    add_run(db, "mace", [edit], [(step("t1", False),)])
    add_run(db, "mace", [invented], [(step("t1", True),)])
    add_run(db, "mace", ["no task lines here"], [(step("t1", True),)])
    return path


def rows(db_path, **kwargs):
    return plan_replay.replay_plans(db_path, **kwargs)


class TestReplayPlans:
    def test_a_plan_without_problems_has_none(self, db_path):
        # The plan with the define, and the one that adds a made-up define beside it (stripped only with a checkout).
        good = [r for r in rows(db_path) if not r.problems and not r.stripped]
        assert len(good) == 2 and all(r.passed is True for r in good)

    def test_a_reply_with_no_task_lines_is_skipped(self, db_path):
        assert len(rows(db_path)) == 5  # six replies, one unparseable

    def test_a_plan_missing_the_required_define_is_flagged_and_its_iteration_failed(self, db_path):
        flagged = [r for r in rows(db_path) if REQUIRED_DEFINE in {p.rule for p in r.problems}]
        assert len(flagged) == 2 and all(r.passed is False for r in flagged)

    def test_the_second_plan_that_repeats_a_failed_default_is_a_repeat(self, db_path):
        repeats = [r for r in rows(db_path) if REPEATED_FAILURE in {p.rule for p in r.problems}]
        assert [(r.iteration, r.passed) for r in repeats] == [(1, False)]

    def test_the_first_plan_is_never_a_repeat(self, db_path):
        assert all(r.iteration > 0 for r in rows(db_path) if REPEATED_FAILURE in {p.rule for p in r.problems})

    def test_an_edit_instruction_is_flagged(self, db_path):
        assert any(SOURCE_EDIT in {p.rule for p in r.problems} for r in rows(db_path))

    def test_repeats_can_be_left_out(self, db_path):
        assert not any(REPEATED_FAILURE in {p.rule for p in r.problems} for r in rows(db_path, repeats_rule=False))

    def test_a_run_labelled_with_rtl_allows_rtl_tasks(self, tmp_path):
        path = str(tmp_path / "rtl.db")
        db = metrics.open_db(path, ray_placement=False)
        add_run(db, "mace_rtl", [f"TASK: fix | deps= | kind=rtl | Fix the file\nCONFIG_RTL: fix | {BIST}\n"], [(step("fix", True),)])
        assert [r.problems for r in rows(path)] == [()]

    def test_the_database_is_left_exactly_as_it_was(self, db_path):
        before = open(db_path, "rb").read()
        rows(db_path)
        assert open(db_path, "rb").read() == before


class TestWithACheckout:
    def make_checkout(self, tmp_path):
        define_index.cache_clear()
        (tmp_path / "piton" / "design").mkdir(parents=True)
        (tmp_path / "piton" / "design" / "x.v").write_text(f"`ifdef {BIST}\n`endif\n")
        return str(tmp_path)

    def test_a_define_no_file_mentions_is_stripped_not_flagged(self, db_path, tmp_path):
        plans = rows(db_path, checkout=self.make_checkout(tmp_path / "co"))
        stripped = [r for r in plans if r.stripped]
        assert [r.stripped for r in stripped] == [("MADE_UP_FLAG",)]
        assert stripped[0].problems == () and stripped[0].passed is True

    def test_without_a_checkout_nothing_is_stripped(self, db_path):
        assert not any(r.stripped for r in rows(db_path))


class TestTally:
    def test_counts_plans_by_rule_and_outcome(self, db_path):
        counts = plan_replay.tally(rows(db_path))
        assert counts["all"] == {"passed": 2, "failed": 3}
        assert counts[REQUIRED_DEFINE]["failed"] == 2 and "passed" not in counts[REQUIRED_DEFINE]
        assert counts["any"]["failed"] >= 2 and counts["any"].get("passed", 0) == 0

    def test_the_database_connection_is_read_only(self, db_path):
        with pytest.raises(sqlite3.OperationalError):
            conn = plan_replay._connect(db_path)
            conn.execute("DELETE FROM runs")
