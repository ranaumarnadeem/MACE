"""mace.integrator -- apply a task DAG, in dependency order.

No LLM lives here: this module only decides *order* and *whether to keep
going*, not what an edit should say. Deciding that is run_mace_step's job
(and, upstream of it, the Planner's).

Two appliers, for two settled design decisions:

* :func:`integrate` -- one shared checkout, fully serial. build_id already
  isolates concurrent *builds* by configuration, and nothing here ever runs
  two builds against the same checkout at once, so one checkout is enough;
  a per-task checkout would protect against a race this function never
  creates.
* :func:`integrate_parallel` -- multiple checkouts, one level of
  independent tasks in flight at a time. This is where that assumption
  actually gets exercised: each checkout gets its own
  ``OpenPitonWorkspaceNode`` (real placement-group-bound dispatch, mirroring
  chia_openpiton's own proven parallel-build acceptance test), so two tasks
  in the same level never share a checkout.
"""

from __future__ import annotations

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import PitonConfig
from mace.loop import run_mace_step
from mace.spec import MaceSpec, StepResult, Task
from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR


def topological_levels(tasks: tuple[Task, ...]) -> tuple[tuple[Task, ...], ...]:
    """*tasks* grouped into levels: level N depends only on levels < N.

    Tasks within one level have no dependency relationship to each other,
    so they may run in parallel; levels themselves must still run in order.
    Deterministic: within a level, relative order in *tasks* is preserved.

    Raises:
        ValueError: a dep id that isn't any task's id, or a dependency cycle.
    """
    by_id = {t.id: t for t in tasks}
    for t in tasks:
        for d in t.deps:
            if d not in by_id:
                raise ValueError(f"task {t.id!r} depends on unknown task {d!r}")

    levels: list[tuple[Task, ...]] = []
    done: set[str] = set()
    remaining = list(tasks)
    while remaining:
        ready = [t for t in remaining if all(d in done for d in t.deps)]
        if not ready:
            stuck = ", ".join(t.id for t in remaining)
            raise ValueError(f"dependency cycle among tasks: {stuck}")
        levels.append(tuple(ready))
        done.update(t.id for t in ready)
        remaining = [t for t in remaining if t.id not in done]
    return tuple(levels)


def topological_order(tasks: tuple[Task, ...]) -> tuple[Task, ...]:
    """*tasks* flattened from :func:`topological_levels`, one after another."""
    return tuple(t for level in topological_levels(tasks) for t in level)


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


def integrate_parallel(
    piton_roots: tuple[str, ...],
    spec: MaceSpec,
    tasks: tuple[Task, ...],
    llm,
    tools=(),
    asm_diag_root: str | None = None,
) -> tuple[StepResult, ...]:
    """Apply *tasks* across *piton_roots* in parallel, one level at a time.

    Each level's tasks are dispatched round-robin, one per checkout, batched
    in groups of ``len(piton_roots)`` when a level has more tasks than
    checkouts available. Every task in a batch is dispatched (prompt, then
    build, then run) before any of that step is resolved -- the same
    dispatch-then-collect shape chia_openpiton's own parallel-build
    acceptance test uses -- so batches genuinely overlap on the cluster
    rather than running one task's whole pipeline before the next starts.

    Stops at the first level containing any failed gate: nothing in a later
    level gets to build on a tree that just failed verification. Returns
    every StepResult produced up to and including that level (or all of
    them, if every task passed).

    ``asm_diag_root`` defaults to mace's own ``workloads/`` directory --
    see run_mace_step's docstring for why that's safe as a default even for
    an OpenPiton-native test name.
    """
    root_dir = str(WORKLOADS_DIR) if asm_diag_root is None else asm_diag_root
    nodes = [OpenPitonWorkspaceNode(root, pg_ready_timeout_s=120) for root in piton_roots]
    try:
        results: list[StepResult] = []
        for level in topological_levels(tasks):
            level_results = _run_level(nodes, spec, level, llm, tools, root_dir)
            results.extend(level_results)
            if not all(r.passed for r in level_results):
                break
        return tuple(results)
    finally:
        for node in nodes:
            node.close()


def _run_level(
    nodes: list, spec: MaceSpec, level: tuple[Task, ...], llm, tools, asm_diag_root: str
) -> list[StepResult]:
    """One level, batched to at most ``len(nodes)`` tasks in flight at once."""
    results: list[StepResult] = []
    tasks = list(level)
    while tasks:
        batch = tasks[: len(nodes)]
        tasks = tasks[len(nodes) :]
        results.extend(_run_batch(nodes[: len(batch)], spec, batch, llm, tools, asm_diag_root))
    return results


def _run_batch(
    nodes: list, spec: MaceSpec, batch: list[Task], llm, tools, asm_diag_root: str
) -> list[StepResult]:
    """One (node, task) pair per entry; prompt, build, run each fully
    dispatched across the batch before any of that round is resolved."""
    config = PitonConfig(core=spec.core, x_tiles=spec.target_mesh[0], y_tiles=spec.target_mesh[1])

    prompt_refs = [llm.prompt.chia_remote(llm, task.spec, list(tools)) for task in batch]
    queries = [get(ref) for ref in prompt_refs]

    build_refs = [node.build.chia_remote(config) for node in nodes]
    builds = [get(ref) for ref in build_refs]

    run_refs = {
        i: nodes[i].run.chia_remote(
            config,
            spec.workloads[0],
            asm_diag_root=asm_diag_root,
            rtl_timeout=RECOMMENDED_RTL_TIMEOUT,
        )
        for i, build in enumerate(builds)
        if build.success
    }
    runs = {i: get(ref) for i, ref in run_refs.items()}

    results: list[StepResult] = []
    for i, task in enumerate(batch):
        run = runs.get(i)
        passed = run.success if run is not None else False
        results.append(
            StepResult(task=task, query=queries[i], build=builds[i], run=run, passed=passed)
        )
    return results
