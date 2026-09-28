"""mace.codesign.run -- one co-design search, recorded like any other run.

Each round asks the strategy for up to ``batch`` new designs, builds and
checks them in parallel through the loop's own build-and-check path
(mace.integrator.integrate_parallel, task prompts off), and scores each: a
design that passes has a finish time, the summed ``sim_time`` of its gate
workloads, and every design has a cache area (see
:mod:`mace.codesign.area`). A design is feasible when it passes and its
area fits the budget. The search stops after ``simulations`` designs, when
the time budget runs out, or when the strategy proposes nothing new twice
in a row.

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
    try:
        while len(history) < simulations and time.monotonic() < deadline:
            round_started = time.monotonic()
            want = min(batch, simulations - len(history))
            seen = {e.design for e in history}
            designs = [d for d in strategy.propose(history, want) if d not in seen][:want]
            if not designs:
                record_llm_calls(db, run_id, round_no, calls.take())
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
                area: Area = area_fn(design, tiles)
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
