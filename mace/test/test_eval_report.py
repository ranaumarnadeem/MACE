"""Tier-0 tests for mace.eval.report.

Run:
    pytest mace/test/test_eval_report.py -q
"""

from __future__ import annotations

import pytest

from chia.base.llm_call import QueryResult
from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics
from mace.eval import report
from mace.spec import MaceSpec, StepResult, Task
from mace.usage import LLMCall


def step(task_id, passed, build_s, run_s, programs=("a.c",)):
    build = PitonBuildArtifact(
        success=True, returncode=0, config=PitonConfig(), sim_type="vlt", model_dir="/x",
        binary_path="/x/V", wall_time_s=build_s,
    )
    runs = tuple(
        PitonRunResult(success=passed, returncode=0, test=p, sim_type="vlt", run_dir="/x",
                       verdict="pass" if passed else "fail", wall_time_s=run_s)
        for p in programs
    )
    query = QueryResult(result="", returncode=0, stderr="", stream_result="", success=True)
    return StepResult(task=Task(id=task_id, deps=(), kind="config", spec="s"), query=query,
                      build=build, run=runs[-1], passed=passed, runs=runs)


def add_run(db, task, method, repeat, status, wall, steps=(), calls=(), resims=()):
    spec = MaceSpec(workloads=("a.c",), objective="o")
    run_id = metrics.start_run(db, spec, labels=metrics.RunLabels(method=method, task=task, repeat=repeat, meta={"model": "m"}))
    for i, results in enumerate(steps):
        metrics.record_iteration(db, run_id, i, results, wall_s=1.0)
    metrics.record_llm_calls(db, run_id, 0, tuple(calls))
    for task_id, run in resims:
        metrics.record_resimulation(db, run_id, task_id, run)
    metrics.finish_run(db, run_id, status)
    db.execute("UPDATE runs SET started_at = 100.0, finished_at = ? WHERE run_id = ?", (100.0 + wall, run_id))
    return run_id


@pytest.fixture
def db(tmp_path):
    db = metrics.open_db(str(tmp_path / "eval.db"), ray_placement=False)
    add_run(db, "t1", "mace", 0, "passed", 100.0, steps=[(step("a", False, 30.0, 5.0),), (step("b", True, 0.0, 6.0),)],
            calls=[LLMCall("plan", input_tokens=10, thinking_tokens=4, usd=0.01), LLMCall("triage", usd=0.02)])
    add_run(db, "t1", "mace", 1, "passed", 140.0, steps=[(step("a", True, 40.0, 5.0, programs=("a.c", "b.c")),)])
    add_run(db, "t1", "retry_agent", 0, "budget_exceeded", 300.0, steps=[(step("design", False, 20.0, 5.0),)])
    add_run(db, "t2", "mace", 0, "passed", 50.0)
    add_run(db, "t2", "retry_agent", 0, "passed", 80.0)
    accepted = PitonRunResult(success=False, returncode=0, test="a.c", sim_type="vlt", run_dir="/x", verdict="timeout")
    add_run(db, "t1", "build_check", 0, "passed", 30.0, steps=[(step("a", True, 25.0, 0.0),)], resims=[("a", accepted)])
    metrics.start_run(db, MaceSpec(workloads=("a.c",), objective="o"))  # an unlabelled run, left out
    return db


class TestRunRows:
    def test_one_row_per_labelled_run(self, db):
        rows = report.run_rows(db)
        assert len(rows) == 6
        first = next(r for r in rows if r["method"] == "mace" and r["repeat"] == 0 and r["task"] == "t1")
        assert first["passed"] is True
        assert first["wall_s"] == 100.0
        assert first["machine_s"] == 41.0
        assert (first["builds"], first["fresh_builds"], first["simulations"]) == (2, 1, 2)
        assert (first["llm_calls"], first["input_tokens"], first["thinking_tokens"]) == (2, 10, 4)
        assert first["usd"] == pytest.approx(0.03)
        assert first["model"] == "m"
        assert first["false_accepts"] is None

    def test_counts_every_program_run(self, db):
        second = next(r for r in report.run_rows(db) if r["method"] == "mace" and r["repeat"] == 1)
        assert second["simulations"] == 2

    def test_false_accepts_come_from_the_resimulations(self, db):
        row = next(r for r in report.run_rows(db) if r["method"] == "build_check")
        assert (row["accepted_designs"], row["false_accepts"]) == (1, 1)


class TestSummaries:
    def test_per_task_and_method(self, db):
        rows = report.summarize(report.run_rows(db))
        t1_mace = next(r for r in rows if (r["task"], r["method"]) == ("t1", "mace"))
        assert (t1_mace["runs"], t1_mace["passed"]) == (2, 2)
        assert t1_mace["time_to_pass_s"] == 120.0
        t1_retry = next(r for r in rows if (r["task"], r["method"]) == ("t1", "retry_agent"))
        assert (t1_retry["passed"], t1_retry["time_to_pass_s"]) == (0, None)
        assert next(r for r in rows if r["method"] == "build_check")["false_accepts"] == 1

    def test_per_method(self, db):
        totals = {r["method"]: r for r in report.method_totals(report.run_rows(db))}
        assert (totals["mace"]["tasks"], totals["mace"]["runs"], totals["mace"]["passed"]) == (2, 3, 3)
        assert totals["retry_agent"]["passed"] == 1

    def test_reading_through_the_sqlite_reader(self, db, tmp_path):
        assert len(report.rows_from([str(tmp_path / "eval.db")])) == 6


class TestWilcoxon:
    def test_all_positive_differences(self):
        w, p = report.wilcoxon_signed_rank([1, 2, 3, 4, 5])
        assert w == 15
        assert p == pytest.approx(2 / 32)

    def test_zero_differences_are_dropped(self):
        assert report.wilcoxon_signed_rank([0, 0]) == (0.0, 1.0)
        w, p = report.wilcoxon_signed_rank([0, 1, 2, 3, 4, 5])
        assert (w, p) == (15, pytest.approx(2 / 32))

    def test_symmetric_differences_give_p_one(self):
        w, p = report.wilcoxon_signed_rank([1, -1, 2, -2])
        assert p == pytest.approx(1.0)

    def test_ties_share_their_average_rank(self):
        w, _ = report.wilcoxon_signed_rank([1, 1, -3])
        assert w == 3.0  # ranks 1.5 + 1.5


class TestPairedTest:
    def test_uses_tasks_where_both_passed(self, db):
        result = report.paired_test(report.run_rows(db), "retry_agent")
        assert result["tasks"] == 1  # only t2: retry_agent never passed t1
        assert result["median_difference"] == 30.0


class TestMarkdown:
    def test_table(self):
        text = report.markdown_table([{"a": 1.234, "b": None, "c": "x"}], ["a", "b", "c"])
        assert text.splitlines() == ["| a | b | c |", "|---|---|---|", "| 1.23 | - | x |"]


class TestCodesignSummary:
    def _search(self, db, method, repeat, finishes):
        from mace.codesign.run import Evaluation
        from mace.codesign.space import Design

        spec = MaceSpec(workloads=("matmul.c",), objective="o")
        run_id = metrics.start_run(db, spec, labels=metrics.RunLabels(method=method, task="cd", repeat=repeat))
        for i, finish in enumerate(finishes):
            metrics.record_evaluation(db, run_id, Evaluation(
                index=i, round=i, design=Design.of({"l1d": (4096 * (i + 1), 2)}), passed=finish is not None,
                feasible=finish is not None, sim_time=finish, area_um2=1.0, read_energy_nj=0.0,
                area_source="analytical", wall_s=1.0,
            ))
        metrics.finish_run(db, run_id, "passed" if any(f is not None for f in finishes) else "budget_exceeded")

    def test_best_found_and_simulations_to_near_best(self, tmp_path):
        db = metrics.open_db(str(tmp_path / "cd.db"), ray_placement=False)
        self._search(db, "codesign_mace", 0, [900, 700, 800])
        self._search(db, "codesign_random", 0, [None, 1000, 720])
        self._search(db, "codesign_random", 1, [None, None, None])
        rows = report.codesign_rows(db)
        assert len(rows) == 3
        mace_row = next(r for r in rows if r["method"] == "codesign_mace")
        assert mace_row["curve"] == [900, 700, 700]
        summary = {s["method"]: s for s in report.codesign_summary(rows)}
        assert summary["codesign_mace"]["best_known"] == 700
        assert (summary["codesign_mace"]["sims_to_near_best"], summary["codesign_mace"]["reached"]) == (2, 1)
        assert (summary["codesign_random"]["reached"], summary["codesign_random"]["sims_to_near_best"]) == (1, 3)
        assert summary["codesign_random"]["feasible_share"] == pytest.approx(2 / 6)
        assert summary["codesign_random"]["best_sim_time"] == 720

    def test_bring_up_rows_leave_out_nothing(self, tmp_path):
        db = metrics.open_db(str(tmp_path / "cd.db"), ray_placement=False)
        self._search(db, "codesign_grid", 0, [500])
        assert report.run_rows(db)[0]["method"] == "codesign_grid"

    def test_a_replaced_search_is_left_out(self, tmp_path):
        db = metrics.open_db(str(tmp_path / "cd.db"), ray_placement=False)
        self._search(db, "codesign_grid", 0, [400])
        db.execute("UPDATE runs SET status = 'running', finished_at = NULL, started_at = started_at - 10")
        self._search(db, "codesign_grid", 0, [500])
        rows = report.codesign_rows(db)
        assert [r["replaced"] for r in rows] == [True, False]
        (summary,) = report.codesign_summary(rows)
        assert (summary["searches"], summary["best_known"]) == (1, 500)


class TestReplacedRuns:
    def test_a_later_run_of_the_same_job_replaces_an_unfinished_one(self, db):
        stale = add_run(db, "t1", "mace", 0, "running", 10.0)
        db.execute("UPDATE runs SET started_at = 50.0, finished_at = NULL WHERE run_id = ?", (stale,))
        rows = report.run_rows(db)
        assert [r["run_id"] for r in rows if r["replaced"]] == [stale]
        t1_mace = next(r for r in report.summarize(rows) if (r["task"], r["method"]) == ("t1", "mace"))
        assert (t1_mace["runs"], t1_mace["passed"]) == (2, 2)
        assert {r["method"]: r["runs"] for r in report.method_totals(rows)}["mace"] == 3

    def test_an_unfinished_run_with_no_later_run_stays(self, db):
        stale = add_run(db, "t3", "mace", 0, "running", 10.0)
        rows = report.run_rows(db)
        assert not next(r for r in rows if r["run_id"] == stale)["replaced"]
        t3 = next(r for r in report.summarize(rows) if r["task"] == "t3")
        assert (t3["runs"], t3["passed"]) == (1, 0)
