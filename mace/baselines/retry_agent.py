"""mace.baselines.retry_agent -- baseline B2: one agent retrying on raw errors.

One LLM agent proposes one design per attempt. Each design is built and
every gate workload simulated on one checkout, through the loop's own
build-and-check path (mace.integrator.integrate_parallel with task prompts
off), so its timeouts, pass check, and records match the loop's. After a
failure the agent gets the raw evidence triage would have read
(:func:`mace.triage.failure_evidence`) and proposes the next design, until a
pass or the budget runs out. With ``rtl_edits``, a design may be an ``rtl``
task, which edits the RTL through the same tool and path the loop's rtl
tasks use.

Against the full loop it lacks the task DAG, the second checkout, and the
triage agent. Its prompt states the run's inputs with the planner's own
:func:`~mace.planner.render_inputs` and asks for overrides with the
planner's :data:`~mace.planner.OVERRIDE_RULES`, and its reply is read with
the planner's parser.
"""

from __future__ import annotations

import dataclasses
import time

from chia.database.sqlite_node import SQLiteNode

from mace import usage
from mace.agents import parse_tasks
from mace.integrator import close_nodes, integrate_parallel, open_nodes
from mace.metrics import (
    RunLabels,
    finish_run,
    mark_all_recovered,
    record_failure,
    record_iteration,
    record_llm_calls,
    start_run,
)
from mace.planner import OVERRIDE_RULES, render_inputs
from mace.spec import LoopOptions, LoopResult, MaceSpec, StepResult, Task
from mace.triage import failure_evidence
from mace.workloads import verify_checksums

PHASE = "agent"
DESIGN_ID = "design"

_HEADER = """\
You are bringing up OpenPiton/{core} to meet one objective. Each attempt is
one design: it is built, and every gate workload is simulated on it. You have
{attempts} attempts in all. After a failed attempt you see what its build and
simulation reported, and you propose the next design.

"""

_DESIGN_RULES = """\
Respond with exactly one task line for the design to build this attempt, in
exactly this format (a footer, not prose):

TASK: design | deps= | kind={kind} | <short description of the design>
{rtl_rule}
Nothing else you write is parsed, but keep the rest brief.
"""


# Added to the design rules when the agent may edit RTL.
_RTL_RULE = """
Use kind=rtl instead when the design needs a change to the RTL source. You
then get tools to read files under piton/design/ and replace text in them,
and the changed design is built and every gate workload simulated. Say in
the description what to change and why. Testbench and monitor files under
piton/verif/ cannot change.
"""


def build_prompt(spec: MaceSpec, history: list[str], rtl_edits: bool = False) -> str:
    """The agent's prompt: the run's inputs, the design format, and every
    earlier attempt's outcome, oldest first."""
    prompt = (
        _HEADER.format(core=spec.core, attempts=spec.budget.max_iterations)
        + render_inputs(spec)
        + "\n"
        + _DESIGN_RULES.format(kind="config|rtl" if rtl_edits else "config", rtl_rule=_RTL_RULE if rtl_edits else "")
        + "\n"
        + OVERRIDE_RULES
    )
    if history:
        prompt += "\nEarlier attempts, oldest first:\n\n" + "\n\n".join(history) + "\n"
    return prompt


def parse_design(text: str) -> Task | None:
    """The design the reply names: its ``design`` task, else its first
    ``config`` or ``workload`` task, else ``None``."""
    tasks = [t for t in parse_tasks(text) if t.kind != "unit_test"]
    if not tasks:
        return None
    chosen = next((t for t in tasks if t.id == DESIGN_ID), tasks[0])
    return dataclasses.replace(chosen, deps=())


def describe_failure(attempt: int, result: StepResult) -> str:
    """One history entry: how attempt number *attempt* failed."""
    verdict = result.run.verdict if result.run is not None else None
    return (
        f"Attempt {attempt} failed (build succeeded: {result.build.success}, run verdict: {verdict}).\n"
        f"What the build and simulation reported:\n{failure_evidence(result)}"
    )


def run_retry_agent(
    piton_root: str,
    spec: MaceSpec,
    llm,
    db: SQLiteNode,
    labels: RunLabels | None = None,
    rtl_edits: bool = False,
) -> LoopResult:
    """Up to ``spec.budget.max_iterations`` attempts on *piton_root*.

    With *rtl_edits*, a design may be an ``rtl`` task, which edits the RTL
    through the loop's own edit tool.

    Stops on the same wall-clock and cost limits the loop checks, before
    each attempt. A reply that names no design uses up its attempt, and
    the agent is told so. The run ends ``passed`` or ``budget_exceeded``,
    or ``checksum_mismatch`` before any attempt when a gate program changed.
    """
    run_id = start_run(db, spec, labels=labels or RunLabels(method="retry_agent"))
    calls = usage.UsageLog()
    try:
        with usage.recording(calls):
            return _attempts(run_id, piton_root, spec, llm, db, calls, rtl_edits)
    except BaseException:
        finish_run(db, run_id, "error")
        raise


def _attempts(run_id, piton_root, spec, llm, db, calls: usage.UsageLog, rtl_edits: bool = False) -> LoopResult:
    try:
        verify_checksums()
    except ValueError:
        finish_run(db, run_id, "checksum_mismatch")
        return LoopResult(run_id=run_id, status="checksum_mismatch", iterations=())

    started = time.monotonic()
    deadline = started + spec.budget.max_wall_s
    history: list[str] = []
    iterations: list[tuple[StepResult, ...]] = []
    status = "budget_exceeded"
    had_a_failure = False
    nodes = None
    try:
        for attempt in range(spec.budget.max_iterations):
            if time.monotonic() - started > spec.budget.max_wall_s:
                break
            if calls.total_usd() > spec.budget.max_usd:
                break
            attempt_started = time.monotonic()
            query = usage.prompt(llm, PHASE, build_prompt(spec, history, rtl_edits))
            design = parse_design(query.result)
            results: tuple[StepResult, ...] = ()
            if design is not None:
                if nodes is None:
                    nodes = open_nodes((piton_root,))
                results = integrate_parallel(
                    (piton_root,), spec, (design,), llm, run_id=run_id, iteration=attempt,
                    nodes=nodes, options=LoopOptions(task_prompts=False, rtl_edits=rtl_edits), deadline=deadline,
                    rtl_context="\n\n".join(history),
                )
            attempt_calls = calls.take()
            iterations.append(results)
            record_iteration(
                db, run_id, attempt, results, time.monotonic() - attempt_started,
                usd=sum(c.usd for c in attempt_calls),
            )
            record_llm_calls(db, run_id, attempt, attempt_calls)
            if design is None:
                history.append(f"Attempt {attempt + 1} named no design: the reply had no well-formed TASK: line.")
                continue
            (result,) = results
            if result.passed:
                status = "passed"
                break
            had_a_failure = True
            record_failure(db, run_id, attempt, result.task.id, "raw_evidence", "")
            history.append(describe_failure(attempt + 1, result))
    finally:
        if nodes is not None:
            close_nodes(nodes)

    if status == "passed" and had_a_failure:
        mark_all_recovered(db, run_id)
    finish_run(db, run_id, status)
    return LoopResult(run_id=run_id, status=status, iterations=tuple(iterations))
