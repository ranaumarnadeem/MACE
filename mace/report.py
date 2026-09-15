"""mace.report -- one LLM call that synthesizes a whole exhausted run into a
single verdict: does this objective look achievable at all?

Mirrors mace.triage's shape (one LLM call, turned into validated structured
output), but where triage diagnoses ONE failed task to inform the next
replan, this synthesizes EVERY task tried across an ENTIRE run that never
reached "passed" -- the report mace.orchestrator.run_mace_loop produces once
its own budget runs out (see run_mace_loop's docstring for exactly which
statuses trigger this).

This exists because the loop's own pass/fail verdicts, by themselves, only
ever say "this didn't work" -- never *why*, and never whether trying a
different configuration would plausibly help versus the objective being
fundamentally out of reach on this hardware (e.g. a core with no coherence
adapter of its own, or a genuine RTL gap in an unvalidated configuration).
Producing that judgment by hand -- comparing against known-good fixtures,
checking symbol tables and entry points, reading which configurations
upstream has ever validated -- is exactly what earlier investigations in
this project did manually (see docs/TECHNICAL_GUIDE.md's PicoRV32 and
2x2-mesh sections). This module is that same reasoning, automated, with the
same read-only tools mace.triage already trusts for log inspection -- and
the same honesty about its limits: an LLM's assessment here is a best-effort
synthesis of the evidence gathered, not a guarantee, exactly like a human's
would be. Nothing here writes to the checkout; that stays triage's rule too.
"""

from __future__ import annotations

from mace.agents import parse_assessment, parse_explanation, parse_next_steps
from mace.spec import MaceSpec, PostMortem, StepResult, Triage

_PROMPT_TEMPLATE = """\
A MACE loop run ended without ever reaching a passing verdict. This is the
final step: synthesize everything that was tried into one clear judgment.

Objective: {objective}
Core: {core}. Target mesh: {x_tiles}x{y_tiles}.
Why the loop stopped: {stop_reason}

Everything tried, in order:

{history}

Based on all of this, is the objective likely still reachable with a
different configuration, or does the evidence point to a genuine hardware/
RTL limitation that configuration changes will not fix? Use the available
log-inspection tools if you need more detail on any specific attempt before
deciding -- do not guess without checking when the tools can tell you.

Respond with exactly these three footer lines (a footer, not prose):

ASSESSMENT: <fixable_config|likely_hardware_limitation|inconclusive>
EXPLANATION: <the specific evidence that led to this assessment>
NEXT_STEPS: <what to try next, or why nothing more is worth trying>
"""


class ReportError(Exception):
    """The post-mortem response produced no assessment."""


def _describe_task(iteration: int, result: StepResult, diagnosis: Triage | None) -> str:
    run = result.run
    verdict = run.verdict if run else None
    line = (
        f"Iteration {iteration}, task {result.task.id} ({result.task.kind}): "
        f"{result.task.spec} -> build_success={result.build.success}, verdict={verdict}"
    )
    if diagnosis is not None:
        line += f"\n  triaged as: {diagnosis.diagnosis} (suggested fix: {diagnosis.fix})"
    return line


def build_prompt(
    spec: MaceSpec,
    iterations: tuple[tuple[StepResult, ...], ...],
    diagnoses: tuple[Triage | None, ...],
    stop_reason: str,
) -> str:
    """The post-mortem prompt for a run that never passed.

    *diagnoses* is one entry per iteration in *iterations* (``None`` where
    that iteration had nothing to triage) -- the same alignment
    mace.orchestrator.run_mace_loop keeps internally between its own
    ``iterations`` and ``diagnoses`` lists.
    """
    lines = []
    for i, (level_results, diagnosis) in enumerate(zip(iterations, diagnoses)):
        for result in level_results:
            lines.append(_describe_task(i, result, diagnosis))
    history = "\n".join(lines) if lines else "(no tasks ever ran)"
    return _PROMPT_TEMPLATE.format(
        objective=spec.objective,
        core=spec.core,
        x_tiles=spec.target_mesh[0],
        y_tiles=spec.target_mesh[1],
        stop_reason=stop_reason,
        history=history,
    )


def generate_post_mortem(
    spec: MaceSpec,
    iterations: tuple[tuple[StepResult, ...], ...],
    diagnoses: tuple[Triage | None, ...],
    stop_reason: str,
    llm,
    tools=(),
) -> PostMortem:
    """One LLM call, turned into a validated final verdict on the whole run.

    Raises:
        ReportError: no ``ASSESSMENT:`` line in the response -- the same
            fail-open posture :func:`mace.triage.triage` documents.
    """
    prompt = build_prompt(spec, iterations, diagnoses, stop_reason)
    query = llm.prompt(prompt, tools=list(tools))
    assessment = parse_assessment(query.result)
    if assessment is None:
        raise ReportError(f"no ASSESSMENT: line in the post-mortem response: {query.result!r}")
    return PostMortem(
        assessment=assessment,
        explanation=parse_explanation(query.result) or "",
        next_steps=parse_next_steps(query.result) or "",
    )
