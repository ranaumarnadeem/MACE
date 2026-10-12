"""mace.eval.directed_report -- the directed-test tables, read from the run databases.

Two questions have an answer here (see
docs/06_mace_evaluation/directed_tests.md):

* Detection (:func:`detection_table`): for each planted bug, how many runs wrote
  a test that passes on the correct design and fails on the bug, how many tests
  were false alarms, and what the runs cost. A test is one ``test_checks`` row.
* Repair (:func:`repair_table`): whether writing a test first changes how often
  and how fast the run repairs the bug, against ``mace_rtl`` and
  ``retry_agent_rtl`` on the same tasks.

Bugs are grouped as ``old`` (the three that break the build, where only false
alarms are defined), ``held-out`` (defined 2026-10-12 before any prompt of the
``test`` task was written), and ``development`` (the bug the prompts were tuned
on, never reported). Every figure comes from the databases; times compare only
within one machine.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict

from mace.eval.report import rows_from, run_rows, summarize
from mace.metrics import DBReader

OLD_BUGS = ("ariane-2x2-hello-rtlfault", "pico-2x2-addi-rtlfault", "ariane-2x2-accu-rtlfault")
HELD_OUT_BUGS = (
    "ariane-2x2-barrier-amobug", "pico-2x2-sh-rtlbug", "pico-2x2-srai-rtlbug", "pico-2x2-mulhsu-rtlbug",
)
OUTCOMES = ("detected", "missed", "false_alarm", "invalid", "unchecked", "runtime_broken")
# The mutation check also runs a null test on every design (mace.eval.directed); its row is not a test.
NULL_TASK = "null"

DETECTION_COLUMNS = [
    "group", "task", "method", "runs", "runs_with_tests", "tests", "detected", "missed", "false_alarm", "invalid",
    "unchecked", "runtime_broken", "runs_detected", "runs_false_alarm", "target_hit", "usd", "method_s",
]
REPAIR_COLUMNS = ["group", "task", "method", "runs", "passed", "time_to_pass_s", "usd", "usd_per_pass"]
TEST_FIRST_COLUMNS = ["group", "task", "runs", "passed", "detected_and_passed", "detected_runs", "other_and_passed", "other_runs"]


def group_of(task: str) -> str:
    """``old``, ``held-out``, or ``development`` for a bug's task id."""
    if task in OLD_BUGS:
        return "old"
    if task in HELD_OUT_BUGS:
        return "held-out"
    return "development"


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _run_facts(db: DBReader) -> dict[str, dict]:
    """Per labelled run: its task, method, repeat, faulted files, the seconds
    its iterations took, and whether a later run replaced it."""
    facts = {}
    for row in run_rows(db):
        meta = db.query_value("SELECT meta FROM runs WHERE run_id = ?", (row["run_id"],))
        faults = (json.loads(meta) if meta else {}).get("source_fault", [])
        seconds = db.query_value("SELECT SUM(wall_s) FROM iterations WHERE run_id = ?", (row["run_id"],))
        facts[row["run_id"]] = {
            "task": row["task"], "method": row["method"], "repeat": row["repeat"], "replaced": row["replaced"],
            "faults": faults, "usd": row["usd"], "method_s": seconds,
        }
    return facts


def check_rows(paths: list[str], with_null: bool = False) -> list[dict]:
    """One row per test the runs in *paths* wrote and the mutation check judged;
    *with_null* adds each run's null-test row."""
    out = []
    for path in paths:
        db = DBReader(path)
        try:
            facts = _run_facts(db)
            rows = db.query(
                "SELECT c.run_id, c.iteration, c.task_id, c.attempt, c.name, c.fault_verdict, c.clean_verdict, "
                "c.outcome, d.target, d.verdict FROM test_checks c LEFT JOIN directed_tests d "
                "ON d.run_id = c.run_id AND d.iteration = c.iteration AND d.task_id = c.task_id "
                "AND d.attempt = c.attempt AND d.name = c.name ORDER BY c.run_id, c.iteration, c.attempt, c.name"
            )
        finally:
            db.close()
        for r in rows:
            run = facts.get(r["run_id"])
            if run is None or run["replaced"] or (r["task_id"] == NULL_TASK and not with_null):
                continue
            out.append(
                {
                    **r, "task": run["task"], "method": run["method"], "repeat": run["repeat"],
                    "target_hit": r["target"] in run["faults"] if r["target"] else None,
                }
            )
    return out


def run_facts_from(paths: list[str]) -> dict[str, dict]:
    """:func:`_run_facts` over every database in *paths*."""
    facts = {}
    for path in paths:
        db = DBReader(path)
        try:
            facts.update(_run_facts(db))
        finally:
            db.close()
    return facts


def detection_table(paths: list[str], methods: tuple[str, ...] = ("testgen", "mace_rtl_test")) -> list[dict]:
    """One row per (task, method): the outcomes of the tests its runs wrote."""
    facts = run_facts_from(paths)
    checks = check_rows(paths)
    runs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for run_id, f in facts.items():
        if not f["replaced"] and f["method"] in methods:
            runs[(f["task"], f["method"])].append(run_id)
    by_run: dict[str, list[dict]] = defaultdict(list)
    for c in checks:
        by_run[c["run_id"]].append(c)
    out = []
    for (task, method), run_ids in sorted(runs.items()):
        tests = [c for run_id in run_ids for c in by_run[run_id]]
        counts = {o: sum(1 for c in tests if c["outcome"] == o) for o in OUTCOMES}
        hits = [c["target_hit"] for c in tests if c["target_hit"] is not None]
        out.append(
            {
                "group": group_of(task), "task": task, "method": method, "runs": len(run_ids),
                "runs_with_tests": sum(1 for r in run_ids if by_run[r]), "tests": len(tests), **counts,
                "runs_detected": sum(1 for r in run_ids if any(c["outcome"] == "detected" for c in by_run[r])),
                "runs_false_alarm": sum(1 for r in run_ids if any(c["outcome"] == "false_alarm" for c in by_run[r])),
                "target_hit": (sum(hits) / len(hits)) if hits else None,
                "usd": _median(facts[r]["usd"] for r in run_ids),
                "method_s": _median(facts[r]["method_s"] for r in run_ids),
            }
        )
    return sorted(out, key=lambda r: (r["group"], r["task"], r["method"]))


def repair_table(paths: list[str], methods: tuple[str, ...] = ("mace_rtl", "mace_rtl_test", "retry_agent_rtl")) -> list[dict]:
    """One row per (task, method): how often the run passed, how long a pass
    took, and what it cost."""
    rows = [r for r in rows_from(paths) if r["method"] in methods]
    out = []
    for s in summarize(rows):
        group = [r for r in rows if r["task"] == s["task"] and r["method"] == s["method"] and not r["replaced"]]
        total = sum(r["usd"] for r in group)
        out.append(
            {
                "group": group_of(s["task"]), "task": s["task"], "method": s["method"], "runs": s["runs"],
                "passed": s["passed"], "time_to_pass_s": s["time_to_pass_s"], "usd": s["usd"],
                "usd_per_pass": (total / s["passed"]) if s["passed"] else None,
            }
        )
    return sorted(out, key=lambda r: (r["group"], r["task"], r["method"]))


def test_first_table(paths: list[str]) -> list[dict]:
    """For ``mace_rtl_test``, per task: of the runs that wrote a detected test
    and of the others, how many passed."""
    facts = run_facts_from(paths)
    status = {}
    for path in paths:
        db = DBReader(path)
        try:
            status.update({r["run_id"]: r["status"] for r in db.query("SELECT run_id, status FROM runs")})
        finally:
            db.close()
    detected = {c["run_id"] for c in check_rows(paths) if c["outcome"] == "detected"}
    groups: dict[str, list[str]] = defaultdict(list)
    for run_id, f in facts.items():
        if f["method"] == "mace_rtl_test" and not f["replaced"]:
            groups[f["task"]].append(run_id)
    out = []
    for task, run_ids in sorted(groups.items()):
        with_test = [r for r in run_ids if r in detected]
        without = [r for r in run_ids if r not in detected]
        out.append(
            {
                "group": group_of(task), "task": task, "runs": len(run_ids),
                "passed": sum(1 for r in run_ids if status[r] == "passed"),
                "detected_runs": len(with_test),
                "detected_and_passed": sum(1 for r in with_test if status[r] == "passed"),
                "other_runs": len(without),
                "other_and_passed": sum(1 for r in without if status[r] == "passed"),
            }
        )
    return sorted(out, key=lambda r: (r["group"], r["task"]))
