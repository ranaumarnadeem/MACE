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
import threading

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
    checkouts available. Within a batch, each task's own prompt -> build ->
    run is pipelined independently of its batch-mates (see ``_run_batch``'s
    own docstring) -- batches genuinely overlap on the cluster, and so does
    each task's progression through its own stages, rather than one slow
    task in any stage holding every other task's next stage hostage.

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
    normally a single task's id, called from whichever of that task's own
    threads reaches its next stage first -- pipelining means two batch-mates
    can genuinely be in different stages (or the same stage) at once, so
    calls are serialized against each other but not batched together. A
    ``unit_test`` task (run locally, not pipelined) is the one case that's
    always exactly one task anyway.

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
    """One (node, task) pair per entry.

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

    Each remote task's own prompt -> build -> run is pipelined on its own
    thread, independently of its batch-mates -- a fast task dispatches its
    build the moment its OWN prompt resolves, not once every task in the
    batch has (the previous shape: collect every prompt, THEN dispatch
    every build, THEN collect every build, THEN dispatch every run -- one
    slow task in any stage held every other task's next stage hostage). The
    batch is still bounded to len(nodes) tasks at a time (one checkout
    each), but within it, wall time now approaches the slowest single
    task's own pipeline, not the sum of each stage's slowest task.
    """
    progress_lock = threading.Lock()

    def _progress(task_ids: tuple[str, ...], stage: str) -> None:
        # Serialized, not just called concurrently: on_task_progress is a
        # caller's callback (chiefly mace.cli's Rich console print) that
        # may not itself be safe to call from multiple threads at once, and
        # pipelining now means several tasks can genuinely enter a stage at
        # the same instant.
        if on_task_progress is not None and task_ids:
            with progress_lock:
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

    def _run_one(node, config: object, task: Task) -> StepResult:
        _progress((task.id,), "prompting")
        query = get(
            llm.prompt.chia_remote(llm, task.spec, list(tools), _chia_tag=_tag(task.id, "prompt"))
        )

        _progress((task.id,), "building")
        build = get(node.build.chia_remote(config, _chia_tag=_tag(task.id, "build")))

        run = None
        if build.success:
            _progress((task.id,), "running")
            run = get(
                node.run.chia_remote(
                    config,
                    spec.workloads[0],
                    asm_diag_root=asm_diag_root,
                    rtl_timeout=RECOMMENDED_RTL_TIMEOUT,
                    _chia_tag=_tag(task.id, "run"),
                )
            )
        passed = run.success if run is not None else False
        return StepResult(task=task, query=query, build=build, run=run, passed=passed)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(remote_batch)) as pool:
        futures = [
            pool.submit(_run_one, node, config, task)
            for node, config, task in zip(remote_nodes, configs, remote_batch)
        ]
        for i, future in zip(remote_indices, futures):
            results[i] = future.result()

    return results
