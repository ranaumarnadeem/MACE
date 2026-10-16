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

import re

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


# How much of a failure's own evidence the re-plan's feedback quotes.
EVIDENCE_DIGEST_CHARS = 800
_NUMBERS = re.compile(r"\d+")


def collapse_similar_lines(lines: list[str]) -> list[str]:
    """*lines* without blanks, where a run of lines that differ only in their
    numbers is its first line and a count: the simulation's sixteen
    ``spc(n) thread(m) -> timeout happen`` lines become one."""
    out: list[str] = []
    previous, first, count = None, "", 0

    def flush() -> None:
        if previous is not None:
            out.append(first if count == 1 else f"{first}  (and {count - 1} similar lines)")

    for line in lines:
        text = line.strip()
        if not text:
            continue
        key = _NUMBERS.sub("#", text)
        if key == previous:
            count += 1
            continue
        flush()
        previous, first, count = key, text, 1
    flush()
    return out


def evidence_digest(result: StepResult, limit: int = EVIDENCE_DIGEST_CHARS) -> str:
    """What a failed task's build or simulation reported, in about *limit*
    characters: the build's error lines, or the failing program's verdict,
    first status line, and the end of its simulation log.

    Triage's prompt carries the long form (:func:`failure_evidence`). The
    re-plan gets this digest as well as triage's label and fix, so a wrong
    diagnosis cannot hide the line that names the cause.
    """
    build, run = result.build, result.run
    if not build.success:
        errors = list(build.errors) or [l for l in (build.stderr or build.stdout).splitlines() if l.strip()][-8:]
        reason = build.failure_reason or "unknown"
        text = f"the build failed ({reason}):\n  " + "\n  ".join(collapse_similar_lines(errors))
        return text if len(text) <= limit else text[:limit].rstrip() + " ..."
    if run is None:
        return "no simulation ran."
    status = next((l.strip() for l in run.status_log.splitlines() if l.strip()), "")
    head = f"{run.test} ended {run.verdict}" + (f" (status: {status})" if status else "") + ":"
    tail = "\n  ".join(collapse_similar_lines(run.sim_log_tail.splitlines()[-12:]))
    room = max(limit - len(head) - 6, 0)
    if len(tail) > room:
        tail = "... " + tail[-room:].lstrip()
    return f"{head}\n  {tail}" if tail else head


def replan_feedback(
    result: StepResult, diagnosis: Triage, raw_evidence: bool = True, narrowing: str = "", probed: bool = False
) -> str:
    """The note a failed task adds to the feedback every later plan sees.

    With *raw_evidence*, it holds the changes from the defaults and the
    build's or simulation's own report (:func:`evidence_digest`) beside
    triage's label and fix, which the note calls a reading of that evidence.
    Without it, the note is the changes, the label, and the fix. *narrowing*
    is :meth:`mace.narrowing.Narrowing.summary`, placed before the diagnosis;
    with *probed*, the diagnosis came from those probes and no LLM call, and
    the note says so. Edits the build carried follow either way.
    """
    changes = changes_from_defaults(result.build.config)
    task = result.task
    extra = f"\n{narrowing}" if narrowing else ""
    if not raw_evidence:
        return (
            f"Task {task.id} ({task.spec}; changes from the defaults: {changes}) failed: "
            f"diagnosis={diagnosis.diagnosis}, suggested fix={diagnosis.fix}"
        ) + extra + edits_feedback(result)
    verdict = result.run.verdict if result.run is not None else None
    source = "Diagnosis from the probes above" if probed else "Triage, an LLM's reading of this evidence"
    return (
        f"Task {task.id} ({task.spec}) failed (build succeeded: {result.build.success}, run verdict: {verdict}).\n"
        f"Changes from the defaults: {changes}\n"
        f"What the build and simulation reported: {evidence_digest(result)}"
        f"{extra}\n"
        f"{source}: diagnosis={diagnosis.diagnosis}, suggested fix={diagnosis.fix}"
    ) + edits_feedback(result)


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
        if result.boot_problems:
            context_parts.append(
                "Boot check: the gate's verdict does not count, since these cores did not start at the core "
                "descriptor's reset vector:\n" + "\n".join(result.boot_problems[:8])
            )
        context_parts.append(f"Sim log (tail):\n{run.sim_log_tail[-1500:]}")
        context_parts.append(f"Status log:\n{run.status_log}")
    # After the errors, so a cut to the evidence's head keeps the file and line.
    if result.edits_diff:
        context_parts.append(f"RTL edits the build carried:\n{result.edits_diff[:EDIT_DIFF_CHARS]}")
    if not result.edit_recorded:
        said = (result.query.result or "").strip()[:EDIT_REPLY_CHARS] if result.query is not None else ""
        context_parts.append(f"This rtl task's agent recorded no edit, so the design was built as it stood. It said: {said}")
    return "\n\n".join(context_parts)


def build_prompt(result: StepResult, objective: str = "", rtl_edits: bool = False, narrowing: str = "") -> str:
    """Triage's prompt for *result*. *objective*, the run's objective,
    leads the prompt when given: it can name what a build needs, such as an
    RTL define, that the failed plan left out. *rtl_edits* says whether the
    next plan may edit RTL, so the suggested fix is one a task can make.
    *narrowing* is what rebuilding the task with its changes undone showed,
    when that settled nothing, so triage knows which causes are ruled out."""
    build = result.build
    run = result.run
    context = failure_evidence(result)
    if narrowing:
        context += "\n\n" + narrowing
    return _PROMPT_TEMPLATE.format(
        objective=f"Run objective: {objective}\n\n" if objective else "",
        task_id=result.task.id,
        kind=result.task.kind,
        spec=result.task.spec,
        build_success=build.success,
        verdict=run.verdict if run else None,
        context=context,
        actions=ACTIONS_RTL if rtl_edits else ACTIONS_NO_RTL,
    )


def triage(
    result: StepResult, llm, tools=(), objective: str = "", rtl_edits: bool = False, narrowing: str = ""
) -> Triage:
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

    query = usage.prompt_retrying(llm, "triage", build_prompt(result, objective, rtl_edits, narrowing), tools)
    diagnosis = parse_diagnosis(query.result)
    if diagnosis is None:
        raise TriageError(f"no DIAGNOSIS: line in the triage response: {query.result!r}")
    return Triage(diagnosis=diagnosis, fix=parse_fix(query.result) or "")
