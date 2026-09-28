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


def changes_from_defaults(config: PitonConfig) -> str:
    """What *config* changes from the mesh's defaults, in words: the RTL
    defines it adds, the cache geometries that differ, and a non-default
    interconnect; ``"none"`` when it changes nothing."""
    parts = []
    added = sorted(set(config.config_rtl) - set(PitonConfig().config_rtl))
    if added:
        parts.append("RTL defines added: " + " ".join(added))
    for name in sorted(config.caches):
        if config.caches[name] != DEFAULT_CACHES.get(name):
            size, assoc = config.caches[name]
            default_size, default_assoc = DEFAULT_CACHES[name]
            parts.append(f"{name}={size},{assoc} (default {default_size},{default_assoc})")
    if config.network_config != PitonConfig().network_config:
        parts.append(f"network {config.network_config}")
    return "; ".join(parts) if parts else "none"


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
    return "\n\n".join(context_parts)


def build_prompt(result: StepResult) -> str:
    build = result.build
    run = result.run
    return _PROMPT_TEMPLATE.format(
        task_id=result.task.id,
        kind=result.task.kind,
        spec=result.task.spec,
        build_success=build.success,
        verdict=run.verdict if run else None,
        context=failure_evidence(result),
    )


def triage(result: StepResult, llm, tools=()) -> Triage:
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

    query = usage.prompt(llm, "triage", build_prompt(result), tools)
    diagnosis = parse_diagnosis(query.result)
    if diagnosis is None:
        raise TriageError(f"no DIAGNOSIS: line in the triage response: {query.result!r}")
    return Triage(diagnosis=diagnosis, fix=parse_fix(query.result) or "")
