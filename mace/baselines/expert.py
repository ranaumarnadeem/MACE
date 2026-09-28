"""mace.baselines.expert -- reference B0: the known passing configuration, run once.

Builds one known passing configuration and runs every gate workload on it,
through the loop's own build-and-check path, with no LLM call. It measures
the least machine time a method can spend on a task. It is a reference,
not a competitor: the human time it took to find the configuration is not
counted.
"""

from __future__ import annotations

import time

from chia.database.sqlite_node import SQLiteNode

from mace.integrator import integrate_parallel
from mace.metrics import RunLabels, finish_run, record_iteration, start_run
from mace.spec import LoopOptions, LoopResult, MaceSpec, Task
from mace.workloads import verify_checksums

EXPERT_TASK_ID = "expert"


def expert_task(
    caches: dict[str, tuple[int, int]] | None = None, config_rtl: tuple[str, ...] | None = None
) -> Task:
    """The expert configuration as a task: *caches* overrides and extra
    *config_rtl* defines on top of the mesh's defaults."""
    return Task(
        id=EXPERT_TASK_ID,
        deps=(),
        kind="config",
        spec="known passing configuration",
        caches=tuple(sorted(caches.items())) if caches else None,
        config_rtl=tuple(config_rtl) if config_rtl else None,
    )


def run_expert(
    piton_roots: tuple[str, ...],
    spec: MaceSpec,
    task: Task,
    db: SQLiteNode,
    labels: RunLabels | None = None,
) -> LoopResult:
    """Build *task*'s configuration on the first checkout and check it.

    The run ends ``passed``, or ``budget_exceeded`` when its one attempt
    fails, matching a loop run whose budget ends without a pass.
    """
    run_id = start_run(db, spec, labels=labels or RunLabels(method="expert"))
    try:
        try:
            verify_checksums()
        except ValueError:
            finish_run(db, run_id, "checksum_mismatch")
            return LoopResult(run_id=run_id, status="checksum_mismatch", iterations=())
        started = time.monotonic()
        results = integrate_parallel(
            piton_roots[:1], spec, (task,), None, run_id=run_id, iteration=0,
            options=LoopOptions(task_prompts=False), deadline=started + spec.budget.max_wall_s,
        )
        record_iteration(db, run_id, 0, results, time.monotonic() - started)
        status = "passed" if results and all(r.passed for r in results) else "budget_exceeded"
        finish_run(db, run_id, status)
        return LoopResult(run_id=run_id, status=status, iterations=(results,))
    except BaseException:
        finish_run(db, run_id, "error")
        raise
