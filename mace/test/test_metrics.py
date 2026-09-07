"""Tier-0 tests for mace.metrics.

Run:
    pytest mace/test/test_metrics.py -q

No Ray needed: open_db(ray_placement=False) uses SQLiteNode's own
no-placement path (require_colocated=False), exactly like
chia/database/test/test_sqlite_node_live.py's local-roundtrip test.
"""

from __future__ import annotations

from chia.base.llm_call import QueryResult
from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics
from mace.spec import MaceSpec, StepResult, Task


def make_spec(**override):
    kwargs = {"workloads": ("hello_world.c",), "objective": "bring up 1x1 ariane"}
    kwargs.update(override)
    return MaceSpec(**kwargs)


def make_result(task_id, passed, wall_s=1.0, kind="workload", verdict="pass"):
    cfg = PitonConfig()
    build = PitonBuildArtifact(
        success=True, returncode=0, config=cfg, sim_type="vlt",
        model_dir="/x", binary_path="/x/Vcmp_top", wall_time_s=wall_s,
    )
    run = PitonRunResult(
        success=passed, returncode=0, test="hello_world.c", sim_type="vlt",
        run_dir="/x/runs/1", verdict=verdict,
    )
    query = QueryResult(result="edit", returncode=0, stderr="", stream_result="edit", success=True)
    task = Task(id=task_id, deps=(), kind=kind, spec="hello_world.c")
    return StepResult(task=task, query=query, build=build, run=run, passed=passed)


def open_test_db(tmp_path):
    return metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)


class TestRunLifecycle:
    def test_start_run_generates_an_id(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        assert run_id

    def test_start_run_accepts_an_explicit_id(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec(), run_id="fixed-id")
        assert run_id == "fixed-id"

    def test_finish_run_updates_status(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        metrics.finish_run(db, run_id, "passed")
        row = db.query_one("SELECT status, finished_at FROM runs WHERE run_id = ?", (run_id,))
        assert row["status"] == "passed"
        assert row["finished_at"] is not None


class TestRecordIteration:
    def test_records_iteration_and_task_rows(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        results = (make_result("a", True), make_result("b", False))

        metrics.record_iteration(db, run_id, 0, results, wall_s=12.0)

        iteration = db.query_one(
            "SELECT * FROM iterations WHERE run_id = ? AND iteration = 0", (run_id,)
        )
        assert (iteration["num_tasks"], iteration["num_passed"]) == (2, 1)
        tasks = db.query(
            "SELECT task_id, passed FROM tasks WHERE run_id = ? ORDER BY task_id", (run_id,)
        )
        assert tasks == [{"task_id": "a", "passed": 1}, {"task_id": "b", "passed": 0}]

    def test_recording_the_same_task_twice_does_not_duplicate(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        results = (make_result("a", True),)

        metrics.record_iteration(db, run_id, 0, results, wall_s=1.0)
        metrics.record_iteration(db, run_id, 0, results, wall_s=1.0)

        count = db.query_value("SELECT COUNT(*) FROM tasks WHERE run_id = ?", (run_id,))
        assert count == 1


class TestFailures:
    def test_record_and_mark_recovered(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())

        metrics.record_failure(db, run_id, 0, "a", "timeout", fix="raise rtl_timeout")
        row = db.query_one("SELECT * FROM failures WHERE run_id = ? AND task_id = 'a'", (run_id,))
        assert (row["diagnosis"], row["recovered"]) == ("timeout", 0)

        metrics.mark_recovered(db, run_id, 0, "a")
        row = db.query_one("SELECT recovered FROM failures WHERE run_id = ? AND task_id = 'a'", (run_id,))
        assert row["recovered"] == 1


class TestSummary:
    def test_aggregates_across_iterations(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())

        metrics.record_iteration(db, run_id, 0, (make_result("a", True), make_result("b", False)), wall_s=10.0, usd=0.5)
        metrics.record_failure(db, run_id, 0, "b", "config_error", recovered=True)
        metrics.record_iteration(db, run_id, 1, (make_result("b", True),), wall_s=5.0, usd=0.2)

        got = metrics.summary(db, run_id)

        assert got == {
            "successful_tasks": 2,
            "iterations": 2,
            "failures_recovered": 1,
            "execution_time_s": 15.0,
            "compute_usd": 0.7,
        }

    def test_empty_run_has_zeroed_summary(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        assert metrics.summary(db, run_id) == {
            "successful_tasks": 0,
            "iterations": 0,
            "failures_recovered": 0,
            "execution_time_s": 0.0,
            "compute_usd": 0.0,
        }
