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

from chia_openpiton.state_def import DEFAULT_CACHES, PitonConfig
from mace import usage
from mace.agents import is_testbench_port_mismatch, parse_diagnosis, parse_fix
from mace.spec import StepResult, Triage

_PROMPT_TEMPLATE = """\
{objective}Task {task_id} ({kind}: {spec}) failed its verification gate.

Build succeeded: {build_success}
Run verdict: {verdict}

{context}

{actions}
Each change from the defaults above is a candidate cause: before blaming the
design, consider whether the failure goes away without a change the
objective does not ask for.

Diagnose why this failed and suggest a fix. Respond with exactly these two
footer lines (a footer, not prose):

DIAGNOSIS: <a short label, e.g. test_bug, config_error, timeout, maxcycles, rtl_suspect, testbench_mismatch>
FIX: <a short, concrete instruction for what to try next>
"""


# What the next plan can change, by whether the run allows RTL edits.
ACTIONS_NO_RTL = (
    "The next plan cannot edit any source file in this run. It can change only the build configuration: "
    "add or remove RTL defines, and set cache sizes and associativity. Suggest a fix of that kind; if no "
    "configuration can fix this failure, say so in FIX and use the diagnosis label needs_rtl_change."
)
ACTIONS_RTL = (
    "The next plan can change the build configuration (RTL defines, cache sizes and associativity) and can "
    "edit files under piton/design/; testbench and monitor files under piton/verif/ cannot change."
)


class TriageError(Exception):
    """The triage response produced no diagnosis."""


def changes_from_defaults(config: PitonConfig) -> str:
    """What *config* changes from the mesh's defaults, in words: the RTL
    defines it adds or leaves out, the cache geometries that differ, and a non-default
    interconnect; ``"none"`` when it changes nothing."""
    parts = []
    default_defines = set(PitonConfig().config_rtl)
    added = sorted(set(config.config_rtl) - default_defines)
    if added:
        parts.append("RTL defines added: " + " ".join(added))
    removed = sorted(default_defines - set(config.config_rtl))
    if removed:
        parts.append("default RTL defines removed: " + " ".join(removed))
    for name in sorted(config.caches):
        if config.caches[name] != DEFAULT_CACHES.get(name):
            size, assoc = config.caches[name]
            default_size, default_assoc = DEFAULT_CACHES[name]
            parts.append(f"{name}={size},{assoc} (default {default_size},{default_assoc})")
    if config.network_config != PitonConfig().network_config:
        parts.append(f"network {config.network_config}")
    return "; ".join(parts) if parts else "none"


# How much of an RTL edit's diff a prompt quotes.
EDIT_DIFF_CHARS = 3000
# How much of an rtl agent's closing words a prompt quotes.
EDIT_REPLY_CHARS = 600


def edits_feedback(result: StepResult) -> str:
    """The RTL edits a failed task's build carried, as a line block to add
    to the re-plan's feedback; "" when it carried none."""
    if not result.edits_diff:
        return ""
    return f"\nThe RTL edits it was built with:\n{result.edits_diff[:EDIT_DIFF_CHARS]}"


def failure_evidence(result: StepResult) -> str:
    """What a failed task's build and simulation reported, capped in length.

    Triage's prompt carries this text. The ``raw`` triage mode and the
    retry-agent baseline hand the same text to their next plan, so they see
    what triage would have read, without its diagnosis.
    """
    build = result.build
    run = result.run
    # The flags show which RTL defines and cache sizes were built, and the
    # changes line picks out the plan's own choices among them, so a failure
    # can be traced to what the plan changed.
    context_parts = [
        f"Build flags: {' '.join(build.config.sims_flags())}",
        f"Changes from the defaults: {changes_from_defaults(build.config)}",
    ]
    if not build.success:
        context_parts.append(f"Build failure reason: {build.failure_reason}")
        if build.errors:
            context_parts.append("Build errors:\n" + "\n".join(build.errors)[:1500])
        # sims prints Verilator's output to stdout and leaves stderr empty.
        output = build.stderr or build.stdout
        context_parts.append(f"Build output (tail):\n{output[-1500:]}")
    elif run is not None:
        context_parts.append(f"Sim log (tail):\n{run.sim_log_tail[-1500:]}")
        context_parts.append(f"Status log:\n{run.status_log}")
    # After the errors, so a cut to the evidence's head keeps the file and line.
    if result.edits_diff:
        context_parts.append(f"RTL edits the build carried:\n{result.edits_diff[:EDIT_DIFF_CHARS]}")
    if not result.edit_recorded:
        said = (result.query.result or "").strip()[:EDIT_REPLY_CHARS] if result.query is not None else ""
        context_parts.append(f"This rtl task's agent recorded no edit, so the design was built as it stood. It said: {said}")
    return "\n\n".join(context_parts)


def build_prompt(result: StepResult, objective: str = "", rtl_edits: bool = False) -> str:
    """Triage's prompt for *result*. *objective*, the run's objective,
    leads the prompt when given: it can name what a build needs, such as an
    RTL define, that the failed plan left out. *rtl_edits* says whether the
    next plan may edit RTL, so the suggested fix is one a task can make."""
    build = result.build
    run = result.run
    return _PROMPT_TEMPLATE.format(
        objective=f"Run objective: {objective}\n\n" if objective else "",
        task_id=result.task.id,
        kind=result.task.kind,
        spec=result.task.spec,
        build_success=build.success,
        verdict=run.verdict if run else None,
        context=failure_evidence(result),
        actions=ACTIONS_RTL if rtl_edits else ACTIONS_NO_RTL,
    )


def triage(result: StepResult, llm, tools=(), objective: str = "", rtl_edits: bool = False) -> Triage:
    """One LLM call, turned into a validated diagnosis.

    Skips that call entirely when the failed task is a ``unit_test`` and the
    build's output (its error lines, stdout, and stderr; sims prints
    Verilator's diagnostics to stdout) already carries the unambiguous
    signature of a testbench/DUT port mismatch (see
    mace.agents.is_testbench_port_mismatch)
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
    output = "\n".join((*build.errors, build.stdout, build.stderr))
    if result.task.kind == "unit_test" and not build.success and is_testbench_port_mismatch(output):
        return Triage(
            diagnosis="testbench_mismatch",
            fix=(
                f"Edit the scaffolded testbench for task {result.task.id!r}'s DUT "
                f"instantiation: it connects a port name the real module doesn't "
                f"have (see the %Error-PINNOTFOUND line(s) in the build stderr for "
                f"which name(s)). Fix the testbench's port connections, not the DUT."
            ),
        )

    query = usage.prompt(llm, "triage", build_prompt(result, objective, rtl_edits), tools)
    diagnosis = parse_diagnosis(query.result)
    if diagnosis is None:
        raise TriageError(f"no DIAGNOSIS: line in the triage response: {query.result!r}")
    return Triage(diagnosis=diagnosis, fix=parse_fix(query.result) or "")
