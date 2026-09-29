"""mace.codesign.run -- one co-design search, recorded like any other run.

Each round asks the strategy for up to ``batch`` new designs. A design's
cache area (see :mod:`mace.codesign.area`) is known before it is built, and
so is whether it keeps :data:`~mace.codesign.space.ARIANE_WAY_RULE`, so a
design over the area budget or against the rule is recorded as infeasible,
with the reason, without a build or a simulation, and does not use up the
search's simulations; the strategy hears about it like any other outcome.
The rest build and run in parallel
through the loop's own build-and-check path
(mace.integrator.integrate_parallel, task prompts off). A design that
passes has a finish time, the summed ``sim_time`` of its gate workloads,
and is feasible. The search stops after ``simulations`` simulated designs,
after ten times that many proposals, when the time budget runs out, or
when the strategy proposes nothing new twice in a row.

The run lands in the loop's database: one iteration per round, one task
per design, and one ``evaluations`` row per design. It ends ``passed`` when
it found at least one feasible design, and ``budget_exceeded`` otherwise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from chia.database.sqlite_node import SQLiteNode

from mace import usage
from mace.codesign.area import Area, design_area
from mace.codesign.space import Design
from mace.integrator import close_nodes, integrate_parallel, open_nodes
from mace.metrics import (
    RunLabels,
    finish_run,
    record_evaluation,
    record_iteration,
    record_llm_calls,
    start_run,
)
from mace.spec import LoopOptions, MaceSpec
from mace.workloads import verify_checksums


@dataclass(frozen=True)
class Evaluation:
    """One design the search built and checked."""

    index: int
    round: int
    design: Design
    passed: bool
    feasible: bool
    sim_time: int | None
    area_um2: float
    read_energy_nj: float
    area_source: str
    wall_s: float
    # False for a design rejected before any build.
    simulated: bool = True
    # Why a design was rejected before any build: over the area budget, or
    # against the way rule; "" for a design that was built.
    rejected: str = ""


@dataclass(frozen=True)
class CodesignResult:
    run_id: str
    status: str
    evaluations: tuple[Evaluation, ...]

    @property
    def best(self) -> Evaluation | None:
        """The feasible design with the soonest finish, or ``None``."""
        feasible = [e for e in self.evaluations if e.feasible and e.sim_time is not None]
        return min(feasible, key=lambda e: (e.sim_time, e.area_um2)) if feasible else None


def run_codesign(
    piton_roots: tuple[str, ...],
    spec: MaceSpec,
    strategy,
    db: SQLiteNode,
    simulations: int = 20,
    batch: int = 2,
    area_budget_um2: float | None = None,
    labels: RunLabels | None = None,
    area_fn=design_area,
) -> CodesignResult:
    """Search with *strategy* for up to *simulations* designs."""
    run_id = start_run(db, spec, labels=labels or RunLabels(method=f"codesign_{strategy.name}"))
    calls = usage.UsageLog()
    try:
        with usage.recording(calls):
            return _search(run_id, piton_roots, spec, strategy, db, simulations, batch, area_budget_um2, area_fn, calls)
    except BaseException:
        finish_run(db, run_id, "error")
        raise


def _search(run_id, piton_roots, spec, strategy, db, simulations, batch, area_budget_um2, area_fn, calls):
    try:
        verify_checksums()
    except ValueError:
        finish_run(db, run_id, "checksum_mismatch")
        return CodesignResult(run_id=run_id, status="checksum_mismatch", evaluations=())

    tiles = spec.target_mesh[0] * spec.target_mesh[1]
    started = time.monotonic()
    deadline = started + spec.budget.max_wall_s
    history: list[Evaluation] = []
    round_no = 0
    idle_rounds = 0
    nodes = None

    def simulated() -> int:
        return sum(1 for e in history if e.simulated)

    try:
        while simulated() < simulations and len(history) < 10 * simulations and time.monotonic() < deadline:
            round_started = time.monotonic()
            want = min(batch, simulations - simulated())
            seen = {e.design for e in history}
            proposed = [d for d in strategy.propose(history, want) if d not in seen][:want]
            areas = {d: area_fn(d, tiles) for d in proposed}
            reasons = {}
            for d in proposed:
                broken = d.way_violations(spec.core)
                if broken:
                    reasons[d] = "; ".join(broken)
                elif area_budget_um2 is not None and areas[d].area_um2 > area_budget_um2:
                    reasons[d] = "over the area budget"
            designs = [d for d in proposed if d not in reasons]
            rejected = []
            for design in (d for d in proposed if d in reasons):
                evaluation = Evaluation(
                    index=len(history), round=round_no, design=design, passed=False, feasible=False,
                    sim_time=None, area_um2=areas[design].area_um2, read_energy_nj=areas[design].read_energy_nj,
                    area_source=areas[design].source, wall_s=0.0, simulated=False, rejected=reasons[design],
                )
                record_evaluation(db, run_id, evaluation)
                history.append(evaluation)
                rejected.append(evaluation)
            if rejected:
                strategy.observe(rejected)
            if not designs:
                record_llm_calls(db, run_id, round_no, calls.take())
                if not rejected:
                    idle_rounds += 1
                    if idle_rounds >= 2:
                        break
                continue
            idle_rounds = 0
            if nodes is None:
                nodes = open_nodes(piton_roots)
            tasks = tuple(d.task(f"d{len(history) + i}") for i, d in enumerate(designs))
            results = integrate_parallel(
                piton_roots, spec, tasks, None, run_id=run_id, iteration=round_no, nodes=nodes,
                options=LoopOptions(task_prompts=False), deadline=deadline,
            )
            round_calls = calls.take()
            record_iteration(
                db, run_id, round_no, results, time.monotonic() - round_started,
                usd=sum(c.usd for c in round_calls),
            )
            record_llm_calls(db, run_id, round_no, round_calls)
            new = []
            for design, result in zip(designs, results):
                area: Area = areas[design]
                sim_time = (
                    sum(run.sim_time or 0 for run in result.runs)
                    if result.passed and all(run.sim_time is not None for run in result.runs)
                    else None
                )
                fits = area_budget_um2 is None or area.area_um2 <= area_budget_um2
                evaluation = Evaluation(
                    index=len(history), round=round_no, design=design, passed=result.passed,
                    feasible=result.passed and sim_time is not None and fits, sim_time=sim_time,
                    area_um2=area.area_um2, read_energy_nj=area.read_energy_nj, area_source=area.source,
                    wall_s=result.build.wall_time_s + sum(run.wall_time_s for run in result.runs),
                )
                record_evaluation(db, run_id, evaluation)
                history.append(evaluation)
                new.append(evaluation)
            strategy.observe(new)
            round_no += 1
    finally:
        if nodes is not None:
            close_nodes(nodes)

    status = "passed" if any(e.feasible for e in history) else "budget_exceeded"
    finish_run(db, run_id, status)
    return CodesignResult(run_id=run_id, status=status, evaluations=tuple(history))
