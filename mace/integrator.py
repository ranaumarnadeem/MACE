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

import concurrent.futures

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from mace.loop import _config_for_task, run_mace_step
from mace.replay import tag_for
from mace.spec import MaceSpec, StepResult, Task
from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR


def open_nodes(piton_roots: tuple[str, ...]) -> list:
    """One ``OpenPitonWorkspaceNode`` per checkout in *piton_roots*, order
    preserved (matching *piton_roots*' own order, regardless of which
    finishes constructing first). A thin wrapper over the constructor --
    exists so a caller managing nodes' lifecycle itself across multiple
    :func:`integrate_parallel` calls against the same checkouts
    (mace.orchestrator.run_mace_loop, across its replan iterations) has one
    seam to call and monkeypatch, instead of reaching into chia_openpiton
    directly.

    Constructed concurrently, not sequentially: each one can block for up
    to its own ``pg_ready_timeout_s`` (120s) waiting on a real Ray
    placement group, independently of every other checkout's, so K
    checkouts should cost close to the slowest single one, not the sum of
    all of them. Not validated against a live multi-checkout Ray cluster
    in the environment this was written in (no cluster was available) --
    confirm on a real ``--piton-root-2`` run before fully trusting the
    speedup claim, though the underlying pattern (independent blocking I/O
    calls off the main thread) is a standard, low-risk one.
    """
    if not piton_roots:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(piton_roots)) as pool:
        return list(
            pool.map(lambda root: OpenPitonWorkspaceNode(root, pg_ready_timeout_s=120), piton_roots)
        )


def close_nodes(nodes: list) -> None:
    """Counterpart to :func:`open_nodes`."""
    for node in nodes:
        node.close()


def topological_levels(tasks: tuple[Task, ...]) -> tuple[tuple[Task, ...], ...]:
    """*tasks* grouped into levels: level N depends only on levels < N.

    Tasks within one level have no dependency relationship to each other,
    so they may run in parallel; levels themselves must still run in order.
    Deterministic: within a level, relative order in *tasks* is preserved.

    Raises:
        ValueError: a duplicate task id, a dep id that isn't any task's id,
            or a dependency cycle.
    """
    by_id = {t.id: t for t in tasks}
    seen: set[str] = set()
    dupes: set[str] = set()
    for t in tasks:
        (dupes if t.id in seen else seen).add(t.id)
    if dupes:
        raise ValueError(f"duplicate task ids: {', '.join(sorted(dupes))}")
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
    run_id: str | None = None,
    iteration: int = 0,
    on_task_progress=None,
    nodes: list | None = None,
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

    ``run_id`` (with ``iteration``) tags every remote dispatch here with
    ``mace.replay.tag_for(run_id, iteration, task.id, phase)`` for
    ``phase`` in ``"prompt"``/``"build"``/``"run"`` -- the one place in this
    codebase these calls are structurally cacheable/replayable (see
    mace.replay's module docstring: a local call, which is all
    mace.loop.run_mace_step and mace.integrator.integrate ever make, can't
    be tagged at all). Omitting ``run_id`` (the default) dispatches
    untagged, exactly as before -- tagging alone does nothing without a
    caller that has also called mace.replay.enable_caching/enable_replay.

    ``on_task_progress``, if given, is called as ``on_task_progress(task_ids,
    stage)`` (``stage`` one of ``"prompting"``, ``"building"``, ``"running"``)
    right *before* each dispatch that can genuinely take a while --
    real-time in-flight feedback for a caller (chiefly ``mace.cli``) that
    would otherwise see nothing at all until a whole batch (a build/run can
    take minutes) or, worse, a whole iteration finishes. ``task_ids`` is
    every task entering that stage together, since a batch dispatches (and
    is only resolved) as one group -- see this function's own docstring on
    why that's the real unit of "in flight" here, not a single task.

    ``nodes``, if given, are already-constructed ``OpenPitonWorkspaceNode``
    instances (one per checkout, matching *piton_roots*' order) to dispatch
    against directly -- this function then neither constructs nor closes
    them, leaving their lifecycle to the caller. This is for a caller
    running *multiple* calls against the same checkouts (mace.orchestrator.
    run_mace_loop, across its replan iterations): building nodes once
    outside that loop avoids paying a fresh Ray placement-group acquire/
    release cycle on every iteration for checkouts that never actually
    change. Omitted (the default, ``None``), this function builds and
    closes its own nodes from *piton_roots*, exactly as before -- the right
    default for a single call.
    """
    owns_nodes = nodes is None
    if owns_nodes:
        nodes = open_nodes(piton_roots)
    try:
        root_dir = str(WORKLOADS_DIR) if asm_diag_root is None else asm_diag_root
        results: list[StepResult] = []
        for level in topological_levels(tasks):
            level_results = _run_level(
                nodes, spec, level, llm, tools, root_dir, run_id, iteration, on_task_progress
            )
            results.extend(level_results)
            if not all(r.passed for r in level_results):
                break
        return tuple(results)
    finally:
        if owns_nodes:
            close_nodes(nodes)


def _run_level(
    nodes: list,
    spec: MaceSpec,
    level: tuple[Task, ...],
    llm,
    tools,
    asm_diag_root: str,
    run_id: str | None,
    iteration: int,
    on_task_progress=None,
) -> list[StepResult]:
    """One level, batched to at most ``len(nodes)`` tasks in flight at once."""
    results: list[StepResult] = []
    tasks = list(level)
    while tasks:
        batch = tasks[: len(nodes)]
        tasks = tasks[len(nodes) :]
        results.extend(
            _run_batch(
                nodes[: len(batch)], spec, batch, llm, tools, asm_diag_root, run_id, iteration,
                on_task_progress,
            )
        )
    return results


def _run_batch(
    nodes: list,
    spec: MaceSpec,
    batch: list[Task],
    llm,
    tools,
    asm_diag_root: str,
    run_id: str | None,
    iteration: int,
    on_task_progress=None,
) -> list[StepResult]:
    """One (node, task) pair per entry; prompt, build, run each fully
    dispatched across the batch before any of that round is resolved.

    Each task gets its own PitonConfig (mace.loop._config_for_task) rather
    than one shared for the whole batch, since a task's own CACHES: line
    (mace.agents.parse_cache_overrides) can give it a different cache
    geometry than its batch-mates -- e.g. one task deliberately building an
    undersized L1D to probe a gate workload, another building the mesh's
    normal default in the same level.

    A ``unit_test``-kind task is run locally instead, via
    mace.loop.run_mace_step against its slot's own checkout (scaffold,
    reconcile, build -- never run; see run_mace_step's own docstring) --
    exactly like mace.integrator.integrate's serial path, and never through
    the remote pipeline below. ``task.spec`` for a unit_test task is a bare
    RTL path, not an edit instruction, and _config_for_task builds the full
    manycore mesh spec.core describes, not the scaffolded single-module
    ``PitonConfig(sys=env_name)`` run_mace_step._run_unit_test_step builds
    -- dispatching it through the remote prompt/build/run calls below would
    silently build and run the wrong thing.
    """

    def _progress(task_ids: tuple[str, ...], stage: str) -> None:
        if on_task_progress is not None and task_ids:
            on_task_progress(task_ids, stage)

    results: list[StepResult | None] = [None] * len(batch)
    remote_indices = [i for i, task in enumerate(batch) if task.kind != "unit_test"]
    for i, (node, task) in enumerate(zip(nodes, batch)):
        if task.kind == "unit_test":
            _progress((task.id,), "building")
            results[i] = run_mace_step(node.piton_root, spec, task, llm, tools=tools)

    if not remote_indices:
        return results

    remote_batch = [batch[i] for i in remote_indices]
    remote_nodes = [nodes[i] for i in remote_indices]
    configs = [_config_for_task(spec, task) for task in remote_batch]

    def _tag(task_id: str, phase: str) -> str | None:
        return tag_for(run_id, iteration, task_id, phase) if run_id is not None else None

    _progress(tuple(task.id for task in remote_batch), "prompting")
    prompt_refs = [
        llm.prompt.chia_remote(llm, task.spec, list(tools), _chia_tag=_tag(task.id, "prompt"))
        for task in remote_batch
    ]
    queries = [get(ref) for ref in prompt_refs]

    _progress(tuple(task.id for task in remote_batch), "building")
    build_refs = [
        node.build.chia_remote(config, _chia_tag=_tag(task.id, "build"))
        for node, config, task in zip(remote_nodes, configs, remote_batch)
    ]
    builds = [get(ref) for ref in build_refs]

    _progress(tuple(remote_batch[j].id for j, b in enumerate(builds) if b.success), "running")
    run_refs = {
        j: remote_nodes[j].run.chia_remote(
            configs[j],
            spec.workloads[0],
            asm_diag_root=asm_diag_root,
            rtl_timeout=RECOMMENDED_RTL_TIMEOUT,
            _chia_tag=_tag(remote_batch[j].id, "run"),
        )
        for j, build in enumerate(builds)
        if build.success
    }
    runs = {j: get(ref) for j, ref in run_refs.items()}

    for j, task in enumerate(remote_batch):
        run = runs.get(j)
        passed = run.success if run is not None else False
        results[remote_indices[j]] = StepResult(
            task=task, query=queries[j], build=builds[j], run=run, passed=passed
        )
    return results
