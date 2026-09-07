"""mace.integrator -- apply a task DAG to one shared checkout, in order.

No LLM lives here: this module only decides *order* and *whether to keep
going*, not what an edit should say. Deciding that is run_mace_step's job
(and, upstream of it, the Planner's).

Design decision this file settles (left open in the original design notes):
per-task checkout vs. one shared checkout serialized by the integrator.
chia_openpiton.state_def.PitonConfig.build_id already isolates concurrent
*builds* by configuration, and this integrator has no fan-out yet (tasks run
one at a time, in dependency order) -- so nothing here ever has two builds
in flight against the same checkout at once, which is the only scenario a
per-task checkout would protect against. One shared checkout is therefore
enough for now; revisit if fan-out (parallel task execution) shows otherwise.
"""

from __future__ import annotations

from mace.loop import run_mace_step
from mace.spec import MaceSpec, StepResult, Task


def topological_order(tasks: tuple[Task, ...]) -> tuple[Task, ...]:
    """*tasks* ordered so every task follows all of its deps.

    Deterministic: among tasks that become ready at the same point, their
    relative order in *tasks* is preserved, so re-running the same parsed
    plan always applies it the same way.

    Raises:
        ValueError: a dep id that isn't any task's id, or a dependency cycle.
    """
    by_id = {t.id: t for t in tasks}
    for t in tasks:
        for d in t.deps:
            if d not in by_id:
                raise ValueError(f"task {t.id!r} depends on unknown task {d!r}")

    ordered: list[Task] = []
    done: set[str] = set()
    remaining = list(tasks)
    while remaining:
        ready = [t for t in remaining if all(d in done for d in t.deps)]
        if not ready:
            stuck = ", ".join(t.id for t in remaining)
            raise ValueError(f"dependency cycle among tasks: {stuck}")
        for t in ready:
            ordered.append(t)
            done.add(t.id)
        remaining = [t for t in remaining if t.id not in done]
    return tuple(ordered)


def integrate(
    piton_root: str, spec: MaceSpec, tasks: tuple[Task, ...], llm, tools=()
) -> tuple[StepResult, ...]:
    """Apply *tasks* to *piton_root*, one at a time, in dependency order.

    Each task is a full :func:`~mace.loop.run_mace_step` (edit, build, run,
    gate) against the same checkout. Stops at the first task that fails its
    gate -- nothing later in the order gets to build on a tree that just
    failed verification. Returns every :class:`~mace.spec.StepResult`
    produced up to and including that failure (or all of them, if every
    task passed).
    """
    results: list[StepResult] = []
    for task in topological_order(tasks):
        result = run_mace_step(piton_root, spec, task, llm, tools=tools)
        results.append(result)
        if not result.passed:
            break
    return tuple(results)
