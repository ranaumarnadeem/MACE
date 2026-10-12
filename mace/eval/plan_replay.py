"""mace.eval.plan_replay -- what the plan check says about recorded plans.

A run database keeps every planner reply (``llm_calls``, phase ``plan``), each
iteration's outcome, and the evidence of every failed task. :func:`replay_plans`
parses each reply, checks it with :func:`mace.plan_check.check_plan` as the
loop would have, and pairs the verdict with what happened when that plan ran.
It costs no LLM call and no build, so a rule can be tried on every earlier
run before a VM spends hours on it.

A seeded fault changes the plan after the planner wrote it, so the reply is
the plan the check would have seen. A plan that passed and has a problem is a
rule that would have rejected a working plan, which is the mistake to look for.

The replay reads each iteration's first planner call, phase ``plan``. In a
database the loop wrote with the plan check on, a plan the check sent back is
followed by a ``plan_retry`` call, and the iteration ran the retry's plan, so
the replay of such a database pairs the rejected plan with the outcome of its
replacement. It is meant for databases written before the check existed.

The database opens read-only and immutable: :class:`mace.metrics.DBReader` adds
tables and columns to an older database, and a backed-up evidence database
must stay as it was.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from mace.agents import parse_tasks
from mace.integrator import topological_levels
from mace.plan_check import PlanProblem, check_plan, define_index, objective_defines, strip_unknown_defines
from mace.planner import inherit_config_rtl
from mace.spec import MaceSpec

_BUILD_FLAGS = "Build flags: "
NOT_STARTED = "budget_exhausted"


@dataclass(frozen=True)
class ReplayedPlan:
    db: str
    run_id: str
    method: str
    task: str | None
    iteration: int
    # Whether every task of that iteration passed; None when the iteration was never recorded.
    passed: bool | None
    problems: tuple[PlanProblem, ...]
    # The defines no file of the checkout mentions, which the loop removes before it checks the plan.
    stripped: tuple[str, ...] = ()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _failed_configs(conn: sqlite3.Connection, run_id: str, before: int) -> dict[tuple[str, ...], str]:
    """The build flags of each task that failed in an iteration before *before*, with how it failed."""
    failed: dict[tuple[str, ...], str] = {}
    rows = conn.execute(
        "SELECT task_id, iteration, build_success, run_verdict, evidence FROM tasks "
        "WHERE run_id = ? AND iteration < ? AND passed = 0 ORDER BY iteration",
        (run_id, before),
    )
    for row in rows:
        evidence = row["evidence"] or ""
        flags_line = next((l for l in evidence.splitlines() if l.startswith(_BUILD_FLAGS)), None)
        if flags_line is None or NOT_STARTED in evidence:
            continue
        how = "its build failed" if not row["build_success"] else f"its simulation ended {row['run_verdict']}"
        failed.setdefault(
            tuple(flags_line[len(_BUILD_FLAGS):].split()),
            f"task {row['task_id']} in iteration {row['iteration'] + 1} ({how})",
        )
    return failed


def replay_plans(db_path: str, checkout: str | None = None, *, repeats_rule: bool = True) -> list[ReplayedPlan]:
    """Every plan reply in *db_path* that parses to a valid task DAG, checked.

    *checkout* is an OpenPiton checkout; with it the replay also strips
    unknown defines and runs the ``missing_module`` rule, as the loop does.
    ``repeats_rule=False`` leaves out ``repeated_failure``.
    """
    known = define_index(checkout) if checkout else None
    path_exists = (lambda p: os.path.exists(os.path.join(checkout, p))) if checkout else None
    conn = _connect(db_path)
    try:
        replayed: list[ReplayedPlan] = []
        runs = conn.execute("SELECT run_id, objective, core, x_tiles, y_tiles, method, task FROM runs").fetchall()
        for run in runs:
            spec = MaceSpec(
                workloads=("replay",), objective=run["objective"], core=run["core"],
                target_mesh=(run["x_tiles"], run["y_tiles"]),
            )
            calls = conn.execute(
                "SELECT iteration, reply FROM llm_calls WHERE run_id = ? AND phase = 'plan' ORDER BY seq",
                (run["run_id"],),
            ).fetchall()
            for call in calls:
                tasks = parse_tasks(call["reply"] or "")
                if not tasks or call["iteration"] is None:
                    continue
                try:
                    topological_levels(tasks)
                except ValueError:
                    continue
                tasks, stripped = strip_unknown_defines(
                    inherit_config_rtl(tasks), known, keep=objective_defines(spec.objective)
                )
                iteration = call["iteration"]
                outcome = conn.execute(
                    "SELECT num_tasks, num_passed FROM iterations WHERE run_id = ? AND iteration = ?",
                    (run["run_id"], iteration),
                ).fetchone()
                passed = None if outcome is None else (outcome["num_tasks"] > 0 and outcome["num_passed"] == outcome["num_tasks"])
                failed = _failed_configs(conn, run["run_id"], iteration) if repeats_rule else None
                problems = check_plan(
                    spec, tasks, rtl_edits="rtl" in (run["method"] or ""), path_exists=path_exists,
                    failed_configs=failed,
                )
                replayed.append(ReplayedPlan(
                    db=os.path.basename(db_path), run_id=run["run_id"], method=run["method"] or "",
                    task=run["task"], iteration=iteration, passed=passed, problems=problems, stripped=stripped,
                ))
        return replayed
    finally:
        conn.close()


def tally(plans: list[ReplayedPlan]) -> dict[str, Counter]:
    """Per rule, how many plans it flagged, split by whether the plan's iteration
    passed (``passed``), failed (``failed``), or was never recorded (``unrun``),
    plus an ``any`` row for plans with at least one problem, ``stripped`` for plans that
    carried a define no file mentions, and ``all`` for every plan."""
    out: dict[str, Counter] = {}
    for plan in plans:
        outcome = "unrun" if plan.passed is None else ("passed" if plan.passed else "failed")
        out.setdefault("all", Counter())[outcome] += 1
        if plan.problems:
            out.setdefault("any", Counter())[outcome] += 1
        if plan.stripped:
            out.setdefault("stripped", Counter())[outcome] += 1
        for rule in {p.rule for p in plan.problems}:
            out.setdefault(rule, Counter())[outcome] += 1
    return out
