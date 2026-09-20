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

Budget: max_iterations, max_wall_s, and max_usd are all enforced, checked
before each iteration starts (not mid-iteration -- there is no task-
cancellation machinery to interrupt one already in flight). The usd tally
is a lower bound, not a full accounting: it sums mace.llm.extract_cost_usd
over each iteration's per-task execution calls only, via the QueryResult
already carried on each StepResult, and does not include the Planner's or
triage's own call cost (adding that means changing plan()/triage()'s
return shape, which every existing caller and test already depends on --
not worth the churn while per-task costs are the dominant term for any
DAG with more than a couple of tasks). extract_cost_usd's own docstring
covers which backends this can see at all.
"""

from __future__ import annotations

import time

from chia.database.sqlite_node import SQLiteNode

from mace.integrator import integrate_parallel
from mace.llm import extract_cost_usd
from mace.metrics import (
    finish_run,
    mark_all_recovered,
    record_failure,
    record_iteration,
    record_post_mortem,
    start_run,
)
from mace.planner import PlanningError, plan
from mace.report import ReportError, generate_post_mortem
from mace.spec import LoopResult, MaceSpec, Triage
from mace.triage import TriageError, triage
from mace.workloads import verify_checksums

# Statuses a post-mortem is worth generating for: the loop genuinely tried
# and ran real tasks but never reached "passed". Excluded on purpose:
# "passed" (nothing to explain), "checksum_mismatch" (an integrity problem,
# not a hardware-capability question), and "planning_failed" (no task
# history exists yet for a post-mortem to synthesize anything from).
_POST_MORTEM_STATUSES = frozenset(("failed", "budget_exceeded"))


def run_mace_loop(
    piton_roots: tuple[str, ...],
    spec: MaceSpec,
    llm,
    db: SQLiteNode,
    tools=(),
    on_iteration=None,
    on_task_progress=None,
) -> LoopResult:
    """Plan, execute, and -- if a task fails its gate -- triage and replan,
    until something passes or the spec's budget runs out.

    ``on_task_progress``, if given, is passed straight through to
    :func:`~mace.integrator.integrate_parallel` -- see its own docstring.
    Real-time in-flight feedback (a batch's tasks entering prompting/
    building/running), unlike ``on_iteration`` below, which only fires once
    an entire iteration -- every level, every batch -- has already finished.

    ``on_iteration``, if given, is called as ``on_iteration(iteration,
    results)`` immediately after each iteration's results are recorded --
    before triage, before the loop decides whether to continue. This is the
    hook a caller (chiefly ``mace.cli``) uses for real incremental progress
    output (which task is building, which is verifying, the actual
    verification log) instead of the caller seeing nothing until the whole
    run finishes. Optional and side-effect-only: its return value is
    ignored, and an exception from it propagates (a broken progress printer
    should not be silently swallowed the way a broken triage response is).

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

    If the run instead ends with status ``"failed"`` or ``"budget_exceeded"``
    *and at least one iteration actually ran* (excludes, e.g., the wall-time
    budget already being exceeded before the first iteration even starts --
    nothing happened yet for a post-mortem to synthesize) -- one more LLM
    call synthesizes the whole run into a final verdict (see
    :func:`~mace.report.generate_post_mortem`): does the evidence look like a
    fixable configuration problem, or a genuine hardware/RTL limitation no
    amount of reconfiguration will fix. This is deliberately *not* generated
    for ``"checksum_mismatch"`` (an integrity problem, not a capability
    question) or ``"planning_failed"`` (no task history exists to
    synthesize). A post-mortem that fails to parse is dropped, not raised --
    the run's own status/iterations are the load-bearing result either way.

    Before any of that: the gate workloads' own integrity is checked first
    (:func:`~mace.workloads.verify_checksums`). They are the loop's pass/
    fail oracle, so a task that edited them (accidentally or otherwise)
    must not be trusted to grade its own work -- the run is recorded and
    stopped immediately with ``status="checksum_mismatch"``, before
    spending a single LLM call or touching any checkout.
    """
    run_id = start_run(db, spec)
    try:
        verify_checksums()
    except ValueError:
        finish_run(db, run_id, "checksum_mismatch")
        return LoopResult(run_id=run_id, status="checksum_mismatch", iterations=())

    started = time.monotonic()
    feedback = ""
    iterations: list[tuple] = []
    diagnoses: list[tuple[str, Triage] | None] = []
    had_a_failure = False
    total_usd = 0.0
    status = "budget_exceeded"

    for iteration in range(spec.budget.max_iterations):
        if time.monotonic() - started > spec.budget.max_wall_s:
            status = "budget_exceeded"
            break
        if total_usd > spec.budget.max_usd:
            status = "budget_exceeded"
            break

        # Started before plan()'s own LLM round-trip, not just
        # integrate_parallel's: the recorded wall_s (and therefore the
        # execution_time_s the paper/README cite) must count real time the
        # same way baseline (b) (examples/baseline_one_shot_llm.py) does --
        # that script's timer starts before its own LLM call too. Starting
        # this after plan() would silently exclude every Planner call's
        # latency, biasing the comparison in this loop's favor.
        iter_started = time.monotonic()
        try:
            tasks = plan(spec, llm, tools=tools, feedback=feedback)
        except PlanningError:
            status = "planning_failed"
            break

        results = integrate_parallel(
            piton_roots, spec, tasks, llm, tools=tools, run_id=run_id, iteration=iteration,
            on_task_progress=on_task_progress,
        )
        iter_wall_s = time.monotonic() - iter_started
        iter_usd = sum(extract_cost_usd(r.query) for r in results)
        total_usd += iter_usd
        iterations.append(results)
        diagnoses.append(None)  # overwritten below if this level gets triaged
        record_iteration(db, run_id, iteration, results, iter_wall_s, usd=iter_usd)
        if on_iteration is not None:
            on_iteration(iteration, results)

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
        diagnoses[-1] = (failed.task.id, diagnosis)
        record_failure(db, run_id, iteration, failed.task.id, diagnosis.diagnosis, diagnosis.fix)
        feedback = (
            f"Task {failed.task.id} ({failed.task.spec}) failed: "
            f"diagnosis={diagnosis.diagnosis}, suggested fix={diagnosis.fix}"
        )

    post_mortem = None
    if status in _POST_MORTEM_STATUSES and iterations:
        try:
            post_mortem = generate_post_mortem(
                spec, tuple(iterations), tuple(diagnoses), status, llm, tools=tools
            )
            record_post_mortem(db, run_id, post_mortem)
        except ReportError:
            pass  # fail-open, matching triage's own posture

    if status == "passed" and had_a_failure:
        mark_all_recovered(db, run_id)
    finish_run(db, run_id, status)
    return LoopResult(
        run_id=run_id, status=status, iterations=tuple(iterations), post_mortem=post_mortem
    )
