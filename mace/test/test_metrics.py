"""Tier-0 tests for mace.metrics.

Run:
    pytest mace/test/test_metrics.py -q

No Ray needed: open_db(ray_placement=False) uses SQLiteNode's own
no-placement path (require_colocated=False), exactly like
chia/database/test/test_sqlite_node_live.py's local-roundtrip test.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from chia.base.llm_call import QueryResult
from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics
from mace.spec import MaceSpec, PostMortem, StepResult, Task


def make_spec(**override):
    kwargs = {"workloads": ("hello_world.c",), "objective": "bring up 1x1 ariane"}
    kwargs.update(override)
    return MaceSpec(**kwargs)


def make_result(task_id, passed, wall_s=1.0, kind="workload", verdict="pass", spec="hello_world.c"):
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
    task = Task(id=task_id, deps=(), kind=kind, spec=spec)
    return StepResult(task=task, query=query, build=build, run=run, passed=passed)


def open_test_db(tmp_path):
    return metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)


class TestCachesColumnMigration:
    def test_opening_a_pre_existing_db_without_the_caches_column_adds_it(self, tmp_path):
        """This project's own real runs/*.db files were created before the
        caches column existed -- CREATE TABLE IF NOT EXISTS is a no-op
        against a tasks table that already exists, so open_db must add the
        column itself, or every one of those real databases would start
        raising "no column named caches" the next time a run used them."""
        db_path = tmp_path / "old.db"
        con = sqlite3.connect(str(db_path))
        con.execute(
            "CREATE TABLE tasks ("
            "run_id TEXT NOT NULL, iteration INTEGER NOT NULL, task_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, spec TEXT NOT NULL, passed INTEGER NOT NULL, "
            "build_success INTEGER NOT NULL, run_verdict TEXT, wall_s REAL NOT NULL DEFAULT 0, "
            "PRIMARY KEY (run_id, iteration, task_id))"
        )
        con.commit()
        con.close()

        db = metrics.open_db(str(db_path), ray_placement=False)
        run_id = metrics.start_run(db, make_spec())
        metrics.record_iteration(db, run_id, 0, (make_result("a", True),), wall_s=1.0)

        row = db.query_one("SELECT caches FROM tasks WHERE run_id = ? AND task_id = 'a'", (run_id,))
        assert row["caches"] is not None

    def test_opening_a_fresh_db_twice_does_not_raise(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        metrics.open_db(str(db_path), ray_placement=False)
        metrics.open_db(str(db_path), ray_placement=False)  # column already added -- must no-op


class TestModuleColumnMigration:
    def test_opening_a_pre_existing_db_without_the_module_column_adds_it(self, tmp_path):
        """Same retrofit as TestCachesColumnMigration, for module: this
        project's own real runs/*.db files predate it too."""
        db_path = tmp_path / "old.db"
        con = sqlite3.connect(str(db_path))
        con.execute(
            "CREATE TABLE tasks ("
            "run_id TEXT NOT NULL, iteration INTEGER NOT NULL, task_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, spec TEXT NOT NULL, passed INTEGER NOT NULL, "
            "build_success INTEGER NOT NULL, run_verdict TEXT, wall_s REAL NOT NULL DEFAULT 0, "
            "caches TEXT, "
            "PRIMARY KEY (run_id, iteration, task_id))"
        )
        con.commit()
        con.close()

        db = metrics.open_db(str(db_path), ray_placement=False)
        run_id = metrics.start_run(db, make_spec())
        metrics.record_iteration(db, run_id, 0, (make_result("a", True),), wall_s=1.0)

        row = db.query_one("SELECT module FROM tasks WHERE run_id = ? AND task_id = 'a'", (run_id,))
        assert row is not None  # column exists and is queryable; value is None for a workload task


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
    def test_records_the_build_s_actual_caches_not_the_task_s_requested_ones(self, tmp_path):
        """Ground truth is what the build actually used (result.build.config
        .caches), not what the task's spec text asked for -- a task claiming
        a cache override that never reached the build (the exact silent gap
        a real run hit, see mace.loop's test for the fix) must show up here
        as the *default* geometry, not the requested one, or this column
        would just repeat the same misleading claim in a new place."""
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        cfg = PitonConfig(caches={"l1d": (128, 1)})
        build = PitonBuildArtifact(
            success=True, returncode=0, config=cfg, sim_type="vlt",
            model_dir="/x", binary_path="/x/Vcmp_top", wall_time_s=1.0,
        )
        run = PitonRunResult(
            success=True, returncode=0, test="hello_world.c", sim_type="vlt",
            run_dir="/x/runs/1", verdict="pass",
        )
        query = QueryResult(result="edit", returncode=0, stderr="", stream_result="edit", success=True)
        task = Task(id="a", deps=(), kind="config", spec="build with a tiny L1D")
        result = StepResult(task=task, query=query, build=build, run=run, passed=True)

        metrics.record_iteration(db, run_id, 0, (result,), wall_s=1.0)

        row = db.query_one("SELECT caches FROM tasks WHERE run_id = ? AND task_id = 'a'", (run_id,))
        assert json.loads(row["caches"]) == {"l1d": [128, 1]}

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

    def test_a_mid_loop_exception_leaves_no_partial_iteration_or_task_rows(self, tmp_path, monkeypatch):
        """Before record_iteration wrapped its writes in db.transaction(), the
        iteration row and each task row were committed by their own
        db.execute() call -- a crash partway through the per-task loop left
        iterations.num_tasks permanently out of sync with the task rows that
        actually landed. Injecting a failure while building the second
        task's row must now leave the whole batch, iteration row included,
        unwritten."""
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        results = (
            make_result("a", True, kind="unit_test", spec="picorv32.v"),
            make_result("b", True, kind="unit_test", spec="l15_pipeline.v.pyv"),
        )
        real_module_name_from_path = metrics.module_name_from_path
        calls = {"n": 0}

        def flaky(spec):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return real_module_name_from_path(spec)

        monkeypatch.setattr("mace.metrics.module_name_from_path", flaky)

        with pytest.raises(RuntimeError, match="boom"):
            metrics.record_iteration(db, run_id, 0, results, wall_s=1.0)

        assert db.query_one("SELECT * FROM iterations WHERE run_id = ?", (run_id,)) is None
        assert db.query("SELECT * FROM tasks WHERE run_id = ?", (run_id,)) == []


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

    def test_mark_all_recovered_covers_every_failure_in_the_run(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        other_run_id = metrics.start_run(db, make_spec())

        metrics.record_failure(db, run_id, 0, "a", "timeout")
        metrics.record_failure(db, run_id, 1, "b", "config_error")
        metrics.record_failure(db, other_run_id, 0, "c", "timeout")

        metrics.mark_all_recovered(db, run_id)

        rows = {r["task_id"]: r["recovered"] for r in db.query("SELECT task_id, recovered FROM failures")}
        assert rows == {"a": 1, "b": 1, "c": 0}  # other_run_id's failure is untouched


class TestPostMortem:
    def test_record_and_get_round_trip(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        pm = PostMortem(
            assessment="likely_hardware_limitation",
            explanation="core never reaches its own trap address",
            next_steps="waveform tracing",
        )

        metrics.record_post_mortem(db, run_id, pm)

        assert metrics.get_post_mortem(db, run_id) == pm

    def test_no_post_mortem_recorded_returns_none(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        assert metrics.get_post_mortem(db, run_id) is None

    def test_recording_twice_replaces_not_duplicates(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        metrics.record_post_mortem(
            db, run_id, PostMortem(assessment="inconclusive", explanation="first pass")
        )
        metrics.record_post_mortem(
            db, run_id, PostMortem(assessment="fixable_config", explanation="second pass")
        )

        assert metrics.get_post_mortem(db, run_id).assessment == "fixable_config"
        count = db.query_value(
            "SELECT COUNT(*) FROM post_mortems WHERE run_id = ?", (run_id,)
        )
        assert count == 1


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


class TestFailureTaxonomy:
    def test_groups_by_diagnosis_with_recovery_counts(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())

        metrics.record_failure(db, run_id, 0, "a", "timeout", recovered=True)
        metrics.record_failure(db, run_id, 1, "b", "timeout", recovered=False)
        metrics.record_failure(db, run_id, 0, "c", "config_error", recovered=True)

        got = metrics.failure_taxonomy(db, run_id)

        assert got == [
            {"diagnosis": "timeout", "total": 2, "recovered": 1},
            {"diagnosis": "config_error", "total": 1, "recovered": 1},
        ]

    def test_no_run_id_covers_the_whole_db(self, tmp_path):
        db = open_test_db(tmp_path)
        run_a = metrics.start_run(db, make_spec())
        run_b = metrics.start_run(db, make_spec())
        metrics.record_failure(db, run_a, 0, "a", "timeout")
        metrics.record_failure(db, run_b, 0, "b", "timeout")

        got = metrics.failure_taxonomy(db)

        assert got == [{"diagnosis": "timeout", "total": 2, "recovered": 0}]

    def test_no_failures_is_an_empty_list(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        assert metrics.failure_taxonomy(db, run_id) == []


class TestAllRuns:
    def test_most_recent_run_first_with_its_summary_metrics(self, tmp_path):
        db = open_test_db(tmp_path)
        older = metrics.start_run(db, make_spec(objective="first"))
        metrics.record_iteration(db, older, 0, (make_result("a", True),), wall_s=1.0, usd=0.1)
        newer = metrics.start_run(db, make_spec(objective="second"))
        metrics.record_iteration(db, newer, 0, (make_result("b", True),), wall_s=2.0, usd=0.2)

        got = metrics.all_runs(db)

        assert [r["run_id"] for r in got] == [newer, older]
        assert got[0]["objective"] == "second"
        assert got[0]["successful_tasks"] == 1
        assert got[0]["execution_time_s"] == 2.0

    def test_empty_db_is_an_empty_list(self, tmp_path):
        db = open_test_db(tmp_path)
        assert metrics.all_runs(db) == []


class TestModuleStatus:
    def test_one_unit_test_task_reports_its_module(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        result = make_result("a", True, kind="unit_test", spec="picorv32.v")

        metrics.record_iteration(db, run_id, 0, (result,), wall_s=1.0)

        assert metrics.module_status(db, run_id) == [
            {
                "module": "picorv32",
                "task_id": "a",
                "iteration": 0,
                "passed": True,
                "build_success": True,
                "run_verdict": "pass",
            }
        ]

    def test_non_unit_test_tasks_are_excluded(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        results = (
            make_result("a", True, kind="workload", spec="hello_world.c"),
            make_result("b", True, kind="config", spec="x_tiles=2"),
        )

        metrics.record_iteration(db, run_id, 0, results, wall_s=1.0)

        assert metrics.module_status(db, run_id) == []

    def test_a_retried_module_reports_only_its_latest_iteration(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        failed = make_result("a", False, kind="unit_test", spec="picorv32.v", verdict="fail")
        fixed = make_result("a2", True, kind="unit_test", spec="picorv32.v")

        metrics.record_iteration(db, run_id, 0, (failed,), wall_s=1.0)
        metrics.record_iteration(db, run_id, 1, (fixed,), wall_s=1.0)

        statuses = metrics.module_status(db, run_id)
        assert len(statuses) == 1
        assert statuses[0]["task_id"] == "a2"
        assert statuses[0]["passed"] is True

    def test_two_tasks_for_the_same_module_in_one_iteration_break_ties_deterministically(
        self, tmp_path
    ):
        """ORDER BY iteration DESC alone leaves two same-iteration rows for
        the same module to SQLite's unspecified tie order -- the secondary
        rowid DESC key must make "whichever was written last" the real,
        repeatable rule, not an accident of storage order.
        """
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        first = make_result("a", False, kind="unit_test", spec="picorv32.v", verdict="fail")
        second = make_result("a2", True, kind="unit_test", spec="picorv32.v")

        metrics.record_iteration(db, run_id, 0, (first, second), wall_s=1.0)

        statuses = metrics.module_status(db, run_id)
        assert len(statuses) == 1
        assert statuses[0]["task_id"] == "a2"
        assert statuses[0]["passed"] is True

    def test_different_modules_are_both_reported(self, tmp_path):
        db = open_test_db(tmp_path)
        run_id = metrics.start_run(db, make_spec())
        results = (
            make_result("a", True, kind="unit_test", spec="picorv32.v"),
            make_result("b", False, kind="unit_test", spec="l15_pipeline.v.pyv", verdict="fail"),
        )

        metrics.record_iteration(db, run_id, 0, results, wall_s=1.0)

        modules = {row["module"] for row in metrics.module_status(db, run_id)}
        assert modules == {"picorv32", "l15_pipeline"}

    def test_scoped_to_the_given_run_id(self, tmp_path):
        db = open_test_db(tmp_path)
        run_a = metrics.start_run(db, make_spec(), run_id="run-a")
        run_b = metrics.start_run(db, make_spec(), run_id="run-b")
        metrics.record_iteration(
            db, run_a, 0, (make_result("a", True, kind="unit_test", spec="picorv32.v"),), wall_s=1.0
        )

        assert metrics.module_status(db, run_b) == []
