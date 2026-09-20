"""mace.triage -- one LLM call that diagnoses a failed task.

Mirrors mace.planner's shape (one LLM call, turned into validated
structured output) for the failure-analysis phase: given a task that
failed its gate, ask an LLM for a DIAGNOSIS and a FIX, to feed back into
the Planner's next call as feedback (see mace.planner.plan's ``feedback``
argument) and mace.loop's replan-on-failure loop.

No write tools belong here -- only inspection (chia_openpiton.tools'
grep/collect over the failed run's own logs). The whole point of triage is
to read what happened, not to touch the checkout; any edit belongs to a
later task the Planner produces from this diagnosis.
"""

from __future__ import annotations

from mace.agents import is_testbench_port_mismatch, parse_diagnosis, parse_fix
from mace.spec import StepResult, Triage

_PROMPT_TEMPLATE = """\
Task {task_id} ({kind}: {spec}) failed its verification gate.

Build succeeded: {build_success}
Run verdict: {verdict}

{context}

Diagnose why this failed and suggest a fix. Respond with exactly these two
footer lines (a footer, not prose):

DIAGNOSIS: <a short label, e.g. test_bug, config_error, timeout, maxcycles, rtl_suspect, testbench_mismatch>
FIX: <a short, concrete instruction for what to try next>
"""


class TriageError(Exception):
    """The triage response produced no diagnosis."""


def build_prompt(result: StepResult) -> str:
    build = result.build
    run = result.run
    context_parts = []
    if not build.success:
        context_parts.append(f"Build failure reason: {build.failure_reason}")
        context_parts.append(f"Build stderr (tail):\n{build.stderr[-1500:]}")
    elif run is not None:
        context_parts.append(f"Sim log (tail):\n{run.sim_log_tail[-1500:]}")
        context_parts.append(f"Status log:\n{run.status_log}")
    return _PROMPT_TEMPLATE.format(
        task_id=result.task.id,
        kind=result.task.kind,
        spec=result.task.spec,
        build_success=build.success,
        verdict=run.verdict if run else None,
        context="\n\n".join(context_parts),
    )


def triage(result: StepResult, llm, tools=()) -> Triage:
    """One LLM call, turned into a validated diagnosis.

    Skips that call entirely when the failed task is a ``unit_test`` and the
    build's own stderr already carries the real, unambiguous signature of a
    testbench/DUT port mismatch (see mace.agents.is_testbench_port_mismatch)
    -- there's nothing for an LLM to diagnose that a mechanical check can't
    already say for certain, and it saves the call. Gated on task kind
    because %Error-PINNOTFOUND is a generic Verilator "port not found"
    error, not unique to a scaffolded unit-test testbench: a ``config``/
    ``workload`` task can hit the identical signature from a real RTL
    regression, and there is no scaffolded testbench to blame it on in that
    case. Every other failure still goes through the LLM, since
    ``test_bug``/``config_error``/``timeout``/``maxcycles``/``rtl_suspect``
    genuinely need judgment this module doesn't have.

    Raises:
        TriageError: no ``DIAGNOSIS:`` line in the response -- "not enough
            to act on", the same fail-open posture mace.agents' parsers and
            mace.planner.plan document.
    """
    build = result.build
    if result.task.kind == "unit_test" and not build.success and is_testbench_port_mismatch(build.stderr):
        return Triage(
            diagnosis="testbench_mismatch",
            fix=(
                f"Edit the scaffolded testbench for task {result.task.id!r}'s DUT "
                f"instantiation: it connects a port name the real module doesn't "
                f"have (see the %Error-PINNOTFOUND line(s) in the build stderr for "
                f"which name(s)). Fix the testbench's port connections, not the DUT."
            ),
        )

    query = llm.prompt(build_prompt(result), tools=list(tools))
    diagnosis = parse_diagnosis(query.result)
    if diagnosis is None:
        raise TriageError(f"no DIAGNOSIS: line in the triage response: {query.result!r}")
    return Triage(diagnosis=diagnosis, fix=parse_fix(query.result) or "")
