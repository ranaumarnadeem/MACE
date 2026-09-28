"""mace.baselines.one_shot -- baseline B1: the planner's first plan, run once.

The loop itself with a one-iteration budget, no triage, and no post-mortem.
The plan comes from the planner's own prompt and inputs, so the only
difference from the full loop is that a failed plan gets no second try.
The same runs serve as the "no re-plan" ablation.
"""

from __future__ import annotations

import dataclasses

from chia.database.sqlite_node import SQLiteNode

from mace.metrics import RunLabels
from mace.orchestrator import run_mace_loop
from mace.spec import LoopOptions, LoopResult, MaceSpec

ONE_SHOT_OPTIONS = LoopOptions(triage="off", post_mortem=False)


def one_shot_spec(spec: MaceSpec) -> MaceSpec:
    """*spec* with a budget of one iteration."""
    return dataclasses.replace(spec, budget=dataclasses.replace(spec.budget, max_iterations=1))


def run_one_shot(
    piton_roots: tuple[str, ...],
    spec: MaceSpec,
    llm,
    db: SQLiteNode,
    labels: RunLabels | None = None,
    task_prompts: bool = True,
) -> LoopResult:
    """One plan, executed once on *piton_roots*, recorded as ``one_shot``."""
    return run_mace_loop(
        piton_roots,
        one_shot_spec(spec),
        llm,
        db,
        labels=labels or RunLabels(method="one_shot"),
        options=dataclasses.replace(ONE_SHOT_OPTIONS, task_prompts=task_prompts),
    )
