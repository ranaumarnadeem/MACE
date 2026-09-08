"""mace.orchestrator -- the full loop: plan, execute, triage, replan, repeat.

Ties together every other piece: mace.planner (Requirement -> Planning),
mace.integrator (Task Decomposition -> Parallel Agent Execution -> RTL/
System Integration -> Verification), mace.triage (Failure Analysis), and
mace.metrics (recording), inside the Iteration loop the design's phase list
names. Lives above mace.planner in the dependency graph -- mace.planner
already imports mace.integrator (for topological_levels), so this had to be
a new module rather than living in either of those or in mace.loop.

Only the first failure in a failing level is triaged (see
mace.integrator.integrate_parallel's own docstring on why a failing level
can contain more than one result) -- the simplest version that still closes
the loop; triaging every simultaneous failure is future work if one turns
out to hide another.

Budget: max_iterations and max_wall_s are enforced (checked before each
iteration starts, not mid-iteration -- there is no task-cancellation
machinery to interrupt one already in flight). max_usd is NOT enforced:
nothing in this project yet reads a real backend's usage/cost, so there is
nothing to check it against. Treat max_usd as not-yet-wired, not as
silently ignored-by-design.
"""

from __future__ import annotations

import time

from chia.database.sqlite_node import SQLiteNode

from mace.integrator import integrate_parallel
from mace.metrics import (
    finish_run,
    mark_all_recovered,
    record_failure,
    record_iteration,
    start_run,
)
from mace.planner import PlanningError, plan
from mace.spec import LoopResult, MaceSpec, Triage
from mace.triage import TriageError, triage


def run_mace_loop(
    piton_roots: tuple[str, ...], spec: MaceSpec, llm, db: SQLiteNode, tools=()
) -> LoopResult:
    """Plan, execute, and -- if a task fails its gate -- triage and replan,
    until something passes or the spec's budget runs out.

    Each iteration: :func:`~mace.planner.plan` produces a task DAG (informed
    by the previous iteration's triage, if any), :func:`~mace.integrator.
    integrate_parallel` executes it, and the iteration is recorded via
    :mod:`mace.metrics`. If every task passed, the loop stops with
    ``status="passed"``. Otherwise the first failure is triaged
    (:func:`~mace.triage.triage`) and its diagnosis/fix become feedback for
    the next :func:`~mace.planner.plan` call.

    If the run does eventually pass, every failure recorded earlier in it
    is marked recovered (see :func:`~mace.metrics.mark_all_recovered` for
    why this is coarser than per-task tracking, and why that's the right
    tradeoff here).
    """
    run_id = start_run(db, spec)
    started = time.monotonic()
    feedback = ""
    iterations: list[tuple] = []
    had_a_failure = False
    status = "budget_exceeded"

    for iteration in range(spec.budget.max_iterations):
        if time.monotonic() - started > spec.budget.max_wall_s:
            status = "budget_exceeded"
            break

        try:
            tasks = plan(spec, llm, tools=tools, feedback=feedback)
        except PlanningError:
            status = "planning_failed"
            break

        iter_started = time.monotonic()
        results = integrate_parallel(piton_roots, spec, tasks, llm, tools=tools)
        iter_wall_s = time.monotonic() - iter_started
        iterations.append(results)
        record_iteration(db, run_id, iteration, results, iter_wall_s)

        if results and all(r.passed for r in results):
            status = "passed"
            break

        failed = next((r for r in results if not r.passed), None)
        if failed is None:
            # integrate_parallel returned nothing to run at all -- no task
            # DAG produced any work, so there's nothing to triage either.
            status = "failed"
            break

        had_a_failure = True
        try:
            diagnosis = triage(failed, llm, tools=tools)
        except TriageError:
            diagnosis = Triage(diagnosis="unknown", fix="retry with more context")
        record_failure(db, run_id, iteration, failed.task.id, diagnosis.diagnosis, diagnosis.fix)
        feedback = (
            f"Task {failed.task.id} ({failed.task.spec}) failed: "
            f"diagnosis={diagnosis.diagnosis}, suggested fix={diagnosis.fix}"
        )

    if status == "passed" and had_a_failure:
        mark_all_recovered(db, run_id)
    finish_run(db, run_id, status)
    return LoopResult(run_id=run_id, status=status, iterations=tuple(iterations))
