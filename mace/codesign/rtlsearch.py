"""mace.codesign.rtlsearch -- a search over RTL parameters, recorded like any other run.

A design here is the value of each searched RTL parameter (see
:mod:`mace.codesign.rtl`). Each round asks the strategy for one design. A
design that adds more storage than the budget allows, or whose edits do not
apply to the checkout, is recorded with the reason and costs no simulation.
Any other design is built with its edits applied and checked on the gate
workloads, and uses up one simulation whether it passes or not.

The build and check run here and not in ``integrate_parallel``, because that
path cannot give an edited build its own build id: ``PitonConfig.source_edits``
is set from the edits' digest, so each distinct design builds into its own
model directory and a design met again reuses its build. The edits are applied
before the build and reverted after the runs, however they end.

A passing design has a finish time, the summed ``sim_time`` of its gate
workloads. Each gate program that prints per-hart cycle counts (see
:func:`chia_openpiton.parse.hart_cycles`) has them recorded with the design.
The search stops after ``simulations`` simulated designs, after ten times that
many proposals, when the time budget runs out, or when the strategy proposes
nothing new twice in a row. It ends ``passed`` when at least one design passed
and ``budget_exceeded`` otherwise.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from dataclasses import dataclass, field

from chia.base.ChiaFunction import get
from chia.base.llm_call import QueryResult
from chia.database.sqlite_node import SQLiteNode
from chia_openpiton import parse
from chia_openpiton.state_def import PitonRunResult
from mace import usage
from mace.codesign import rtl
from mace.codesign.rtl import RtlDesign, RtlSpace
from mace.integrator import (
    BUILD_TIMEOUT_S,
    RUN_TIMEOUT_S,
    _not_built,
    _not_run,
    close_nodes,
    edits_digest,
    open_nodes,
    timeout_before,
)
from mace.loop import _config_for_task, run_gate_programs
from mace.metrics import (
    RunLabels,
    finish_run,
    record_iteration,
    record_llm_calls,
    record_rtl_evaluation,
    start_run,
)
from mace.planner import render_inputs
from mace.spec import MaceSpec, StepResult, Task
from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR, verify_checksums

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RtlEvaluation:
    """One design the search tried."""

    index: int
    round: int
    design: RtlDesign
    passed: bool
    feasible: bool
    sim_time: int | None
    storage_bits: int
    wall_s: float
    # False for a design rejected before any simulation.
    simulated: bool = True
    # Why a design was rejected: over the storage budget, an edit that does
    # not apply, or a build that lacks the edit; "" for a simulated design.
    rejected: str = ""
    # {program: {hart: (start, end)}} for each gate program that printed them.
    hart_cycles: dict = field(default_factory=dict)
    # The proposer's bottleneck line for this design, or "".
    note: str = ""


@dataclass(frozen=True)
class RtlSearchResult:
    run_id: str
    status: str
    evaluations: tuple[RtlEvaluation, ...]

    @property
    def best(self) -> RtlEvaluation | None:
        """The feasible design with the soonest finish, or ``None``."""
        feasible = [e for e in self.evaluations if e.feasible and e.sim_time is not None]
        return min(feasible, key=lambda e: (e.sim_time, e.storage_bits)) if feasible else None


# --- the MACE proposer ---------------------------------------------------------

_HEADER = """\
You are tuning RTL parameters of an OpenPiton/{core} {x}x{y} mesh so that the gate
workloads finish soonest in simulation. A design counts only if every tile passes.

"""

_RULES = """\
RTL parameters you may change. Each is a value written into the RTL source; every
other part of the design stays as it is:
{space}

{budget}Tiles are numbered row by row: hart h is on tile (x, y) = (h mod {x}, h div {x}).
A program that times itself prints each hart's mcycle at the start and at the end of
its work. The finish time is the testbench's simulation time when the last hart ends,
so one slow hart sets it. The finish time and the per-hart cycles are in different
units: compare harts with each other.

Propose the next design. End your reply with these two lines:

BOTTLENECK: <one sentence on what the results and per-hart timing so far say limits the finish time>
DESIGN: <name>=<value> ... (every parameter above, using only the listed values)

Do not repeat a design already tried. Nothing else you write is parsed, but keep the rest brief.
"""

_DESIGN_LINE = re.compile(r"(?im)^\s*\**\s*DESIGN:\s*(.+?)\s*$")
_BOTTLENECK_LINE = re.compile(r"(?im)^\s*\**\s*BOTTLENECK:\s*(.+?)\s*$")


def _changes(design: RtlDesign) -> str:
    changed = design.changed()
    return " ".join(f"{n}={v}" for n, v in sorted(changed.items())) if changed else "the default design"


def hart_table(hart_cycles: dict, mesh: tuple[int, int]) -> str:
    """Each gate program's per-hart cycles as text: hart, tile, start, end, and
    the window between them, then the spread."""
    lines = []
    for program, by_hart in hart_cycles.items():
        if not by_hart:
            continue
        lines.append(f"{program}: hart tile start end window")
        windows = {}
        for hart, (start, end) in sorted(by_hart.items()):
            tile = (hart % mesh[0], hart // mesh[0])
            windows[hart] = end - start
            lines.append(f"  {hart} {tile} {start} {end} {end - start}")
        slowest = max(windows, key=windows.get)
        fastest = min(windows, key=windows.get)
        starts = [s for s, _ in by_hart.values()]
        ends = [e for _, e in by_hart.values()]
        lines.append(
            f"  {len(by_hart)} of {mesh[0] * mesh[1]} harts reported; latest end minus earliest start "
            f"{max(ends) - min(starts)}; slowest window hart {slowest} {(slowest % mesh[0], slowest // mesh[0])} "
            f"({windows[slowest]}), fastest hart {fastest} ({windows[fastest]})"
        )
    return "\n".join(lines)


class RtlProposer:
    """MACE's proposer for an RTL-parameter search: one LLM call per round,
    recorded under ``propose``, except the first, which is the default design."""

    name = "mace"
    phase = "propose"

    def __init__(self, llm, spec: MaceSpec, space: RtlSpace, storage_budget_bits: int | None):
        self.llm = llm
        self.spec = spec
        self.space = space
        self.storage_budget_bits = storage_budget_bits
        # The bottleneck line that came with each proposed design.
        self.notes: dict[RtlDesign, str] = {}

    def build_prompt(self, history: list) -> str:
        x, y = self.spec.target_mesh
        bits = self.storage_budget_bits
        if bits is None:
            budget = ""
        else:
            if bits > 0:
                limit = f"may add at most {bits} flip-flop bits per tile over the default design"
            elif bits == 0:
                limit = "may not add flip-flop bits per tile over the default design, so a larger buffer must be paid for with a smaller one"
            else:
                limit = f"must save at least {-bits} flip-flop bits per tile against the default design"
            budget = (
                f"Storage budget: a design {limit}, from each parameter's cost above; a smaller buffer gives bits "
                "back. A design over budget is rejected without a simulation.\n\n"
            )
        prompt = (
            _HEADER.format(core=self.spec.core, x=x, y=y)
            + render_inputs(self.spec)
            + "\n"
            + _RULES.format(space=self.space.describe(), budget=budget, x=x)
        )
        rows = []
        for number, e in enumerate(history, start=1):
            if e.rejected:
                rows.append(f"{number}. {e.design.describe()}: rejected without a simulation ({e.rejected})")
                continue
            outcome = "passed" if e.passed else "failed the check"
            finish = e.sim_time if e.sim_time is not None else "-"
            rows.append(
                f"{number}. {_changes(e.design)}: {outcome}, finish time {finish}, "
                f"{e.storage_bits:+d} storage bits"
            )
        prompt += "\nDesigns tried so far, in order:\n" + "\n".join(rows) + "\n"
        simulated = [e for e in history if not e.rejected and e.hart_cycles]
        shown = {}
        passing = [e for e in simulated if e.feasible and e.sim_time is not None]
        if passing:
            best = min(passing, key=lambda e: e.sim_time)
            shown[best.index] = "best finish time so far"
        if simulated:
            shown.setdefault(simulated[-1].index, "most recent")
        for e in simulated:
            if e.index in shown:
                prompt += (
                    f"\nPer-hart cycles of design {e.index + 1} ({shown[e.index]}; "
                    f"{_changes(e.design)}):\n{hart_table(e.hart_cycles, self.spec.target_mesh)}\n"
                )
        return prompt

    def propose(self, history: list, n: int) -> list[RtlDesign]:
        if not history:
            return [self.space.default()]
        query = usage.prompt(self.llm, self.phase, self.build_prompt(history))
        seen = {e.design for e in history}
        picked: list[RtlDesign] = []
        for line in _DESIGN_LINE.findall(query.result):
            design = rtl.parse_design(line, self.space)
            if design is None or design in seen or design in picked:
                continue
            picked.append(design)
        picked = picked[:n]
        found = _BOTTLENECK_LINE.findall(query.result)
        for design in picked:
            self.notes[design] = found[0] if found else ""
        return picked

    def observe(self, evaluations: list) -> None:
        pass


# --- the search loop -----------------------------------------------------------


def run_rtlsearch(
    piton_roots: tuple[str, ...],
    spec: MaceSpec,
    strategy,
    db: SQLiteNode,
    simulations: int = 10,
    storage_budget_bits: int | None = None,
    labels: RunLabels | None = None,
) -> RtlSearchResult:
    """Search with *strategy* for up to *simulations* designs. Only the first
    of *piton_roots* is used: the designs are checked one at a time."""
    run_id = start_run(db, spec, labels=labels or RunLabels(method=f"rtlparam_{strategy.name}"))
    calls = usage.UsageLog()
    try:
        with usage.recording(calls):
            return _search(run_id, piton_roots[0], spec, strategy, db, simulations, storage_budget_bits, calls)
    except BaseException:
        finish_run(db, run_id, "error")
        raise


def _search(run_id, root, spec, strategy, db, simulations, storage_budget_bits, calls):
    try:
        verify_checksums()
    except ValueError:
        finish_run(db, run_id, "checksum_mismatch")
        return RtlSearchResult(run_id=run_id, status="checksum_mismatch", evaluations=())

    deadline = time.monotonic() + spec.budget.max_wall_s
    history: list[RtlEvaluation] = []
    round_no = 0
    idle_rounds = 0
    nodes = None

    def simulated() -> int:
        return sum(1 for e in history if e.simulated)

    def record(evaluation: RtlEvaluation) -> None:
        record_rtl_evaluation(db, run_id, evaluation)
        history.append(evaluation)
        strategy.observe([evaluation])

    try:
        while simulated() < simulations and len(history) < 10 * simulations and time.monotonic() < deadline:
            round_started = time.monotonic()
            seen = {e.design for e in history}
            proposed = [d for d in strategy.propose(history, 1) if d not in seen][:1]
            if not proposed:
                record_llm_calls(db, run_id, round_no, calls.take())
                idle_rounds += 1
                if idle_rounds >= 2:
                    break
                continue
            idle_rounds = 0
            design = proposed[0]
            note = getattr(strategy, "notes", {}).get(design, "")
            bits = design.storage_delta()

            def rejected(reason: str, wall_s: float = 0.0) -> RtlEvaluation:
                return RtlEvaluation(
                    index=len(history), round=round_no, design=design, passed=False, feasible=False,
                    sim_time=None, storage_bits=bits, wall_s=wall_s, simulated=False, rejected=reason, note=note,
                )

            if storage_budget_bits is not None and bits > storage_budget_bits:
                record(rejected("over the storage budget"))
                record_llm_calls(db, run_id, round_no, calls.take())
                continue
            try:
                edits = rtl.edits_for(design, rtl.read_in(root))
            except (ValueError, OSError) as e:
                record(rejected(f"an edit does not apply: {e}"))
                record_llm_calls(db, run_id, round_no, calls.take())
                continue

            if nodes is None:
                nodes = open_nodes((root,))
            step, harts, missing = _check(nodes[0], root, spec, design, edits, f"d{len(history)}", deadline)
            round_calls = calls.take()
            if missing:
                record(rejected(f"the build lacks the edit in {missing}", step.build.wall_time_s))
            else:
                record_iteration(
                    db, run_id, round_no, (step,), time.monotonic() - round_started,
                    usd=sum(c.usd for c in round_calls),
                )
                sim_time = (
                    sum(run.sim_time or 0 for run in step.runs)
                    if step.passed and all(run.sim_time is not None for run in step.runs)
                    else None
                )
                record(
                    RtlEvaluation(
                        index=len(history), round=round_no, design=design, passed=step.passed,
                        feasible=step.passed and sim_time is not None, sim_time=sim_time, storage_bits=bits,
                        wall_s=step.build.wall_time_s + sum(run.wall_time_s for run in step.runs),
                        hart_cycles=harts, note=note,
                    )
                )
            record_llm_calls(db, run_id, round_no, round_calls)
            round_no += 1
    finally:
        if nodes is not None:
            close_nodes(nodes)

    status = "passed" if any(e.feasible for e in history) else "budget_exceeded"
    finish_run(db, run_id, status)
    return RtlSearchResult(run_id=run_id, status=status, evaluations=tuple(history))


def _check(node, root, spec, design, edits, task_id, deadline):
    """Build *design* with *edits* applied and run the gate programs on it.

    Returns ``(step, harts, missing)``: the build and runs as a StepResult,
    each gate program's per-hart cycles, and the generated file that lacks
    the edit ("" if none does).
    """
    task = Task(id=task_id, deps=(), kind="config", spec=design.describe())
    config = _config_for_task(spec, task)
    query = QueryResult(result="", returncode=0, stderr="", stream_result="", success=True)
    # Edits a killed run left would otherwise be built here.
    get(node.revert_edits.chia_remote())
    applied = None
    try:
        if edits:
            applied = get(node.apply_edits.chia_remote(edits))
            config = dataclasses.replace(config, source_edits=edits_digest(edits))
        build_timeout = timeout_before(deadline, BUILD_TIMEOUT_S)
        if build_timeout == 0:
            build = _not_built(config)
        else:
            build = get(node.build.chia_remote(config, clean=False, timeout_seconds=build_timeout))
        if build.success:
            missing = _missing_from_build(root, design)
            if missing:
                return StepResult(task=task, query=query, build=build, run=None, passed=False), {}, missing

        def run_program(program: str) -> PitonRunResult:
            run_timeout = timeout_before(deadline, RUN_TIMEOUT_S)
            if run_timeout == 0:
                return _not_run(program)
            finish_mask, run_args = spec.run_options(program)
            return get(
                node.run.chia_remote(
                    config, program, asm_diag_root=str(WORKLOADS_DIR),
                    rtl_timeout=spec.rtl_timeout or RECOMMENDED_RTL_TIMEOUT, max_cycle=spec.max_cycle,
                    finish_mask=finish_mask, extra_run_args=run_args, timeout_seconds=run_timeout,
                )
            )

        if not build.success:
            return StepResult(task=task, query=query, build=build, run=None, passed=False), {}, ""
        runs, passed = run_gate_programs(run_program, spec.workloads)
        step = StepResult(task=task, query=query, build=build, run=runs[-1], passed=passed, runs=runs)
        if applied:
            step.edits, step.edits_diff = applied["edits"], applied["diff"]
        return step, _hart_cycles(runs), ""
    finally:
        if applied is not None:
            get(node.revert_edits.chia_remote())


def _missing_from_build(root: str, design: RtlDesign) -> str:
    """The first generated file that does not hold the text *design*'s edits
    should have put there, or ""."""
    read = rtl.read_in(root)
    for path, text in design.generated_checks():
        try:
            if text not in read(path):
                return path
        except OSError:
            return path
    return ""


def _hart_cycles(runs) -> dict:
    """``{program: {hart: (start, end)}}`` for the runs that printed per-hart cycles."""
    found = {}
    for run in runs:
        try:
            harts = parse.hart_cycles(run.fake_uart)
        except ValueError as e:
            logger.warning("%s: unreadable per-hart cycles: %s", run.test, e)
            continue
        if harts:
            found[run.test] = harts
    return found
