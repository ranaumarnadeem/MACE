"""mace.eval.suite -- the evaluation's task suite, loaded from YAML.

A suite file lists tasks. Each names a core, a mesh, the gate workloads,
the objective every method is given, and the expert configuration, the
known passing configuration the expert reference builds. ``defaults``
holds a budget every task starts from. ``verified: false`` marks a
candidate task whose expert configuration has not passed yet; the batch
runner skips those unless asked. ::

    version: 1
    defaults:
      budget: {max_iterations: 3, max_wall_s: 3600}
    tasks:
      - id: pico-2x2-addi
        core: pico
        mesh: [2, 2]
        workloads: [addi.S]
        objective: Verify the addi.S gate workload passes on a 2x2 pico mesh.
        verified: true
        expert:
          config_rtl: [CONFIG_DISABLE_BIST_CLEAR]

A co-design task adds a ``codesign`` block: the design space, the grid the
grid search sweeps, the number of simulations per search, the batch size,
and the area budget as a ratio of the default caches' area. ::

    codesign:
      simulations: 20
      batch: 2
      area_budget_ratio: 1.0
      space:
        l1d: {sizes: [4096, 8192, 16384], assocs: [2, 4, 8]}
        l15: {sizes: [8192, 16384], assocs: [4]}
      grid:
        l1d_size: [4096, 8192, 16384]
        l15_size: [8192, 16384]

``workload_args`` gives a gate program the finish mask and extra ``sims``
run arguments its run needs, as OpenSPARC T1's multi-thread diags do. A
``finish_mask`` left out keeps the mesh's one-per-tile default. ::

    workload_args:
      tso_mutex1.s:
        finish_mask: "3333"
        run_args: [-midas_args=-DTHREAD_COUNT=8]

``networks`` in the space lists the interconnects a search may choose, the
mesh alone by default. ``xbar_config`` needs a mesh with one row of tiles:
OpenPiton's crossbar has one port per column, so a second row would connect
two tiles to the same port.

Loading checks every field, and builds each expert configuration as a
``PitonConfig``, so a broken entry fails when the suite loads, before any
job starts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from chia_openpiton.state_def import DEFAULT_CACHES, PitonConfig
from mace.baselines.expert import expert_task
from mace.codesign.area import cache_arrays
from mace.codesign.space import DEFAULT_NETWORK, Design, DesignSpace
from mace.spec import Budget, MaceSpec, Task

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_TASK_KEYS = frozenset(
    ("id", "core", "mesh", "workloads", "objective", "verified", "expert", "budget", "rtl_timeout", "max_cycle",
     "codesign", "workload_args")
)
_CODESIGN_KEYS = frozenset(("simulations", "batch", "area_budget_ratio", "space", "grid"))


class SuiteError(ValueError):
    """The suite file is malformed; the message names the task and field."""


@dataclass(frozen=True)
class CodesignConfig:
    """What a co-design task searches, and with how many simulations."""

    space: DesignSpace
    grid: dict[str, tuple]
    simulations: int = 20
    batch: int = 2
    # The area budget as a multiple of the default caches' area; None sets
    # no budget.
    area_budget_ratio: float | None = 1.0

    def grid_designs(self) -> list[Design]:
        return self.space.grid(self.grid)

    def default_design(self) -> Design:
        """OpenPiton's default caches and mesh: the area budget's reference."""
        return Design.of({}, DEFAULT_NETWORK)


@dataclass(frozen=True)
class SuiteTask:
    """One evaluation task."""

    id: str
    core: str
    mesh: tuple[int, int]
    workloads: tuple[str, ...]
    objective: str
    budget: Budget
    verified: bool = False
    rtl_timeout: int | None = None
    max_cycle: int | None = None
    expert_caches: dict[str, tuple[int, int]] = field(default_factory=dict)
    expert_config_rtl: tuple[str, ...] = ()
    codesign: CodesignConfig | None = None
    workload_args: tuple[tuple[str, str, tuple[str, ...]], ...] = ()

    @property
    def kind(self) -> str:
        """``codesign`` for a task with a ``codesign`` block, else ``bringup``."""
        return "codesign" if self.codesign is not None else "bringup"

    def spec(self) -> MaceSpec:
        """The run specification every method gets for this task."""
        return MaceSpec(
            workloads=self.workloads,
            objective=self.objective,
            core=self.core,
            target_mesh=self.mesh,
            budget=self.budget,
            rtl_timeout=self.rtl_timeout,
            max_cycle=self.max_cycle,
            workload_args=self.workload_args,
        )

    def expert_task(self) -> Task:
        """The expert configuration, as the task the expert reference builds."""
        return expert_task(caches=self.expert_caches or None, config_rtl=self.expert_config_rtl or None)


def load_suite(path: str | Path) -> tuple[SuiteTask, ...]:
    """Every task in the suite file at *path*, checked.

    Raises:
        SuiteError: an unknown version, a duplicate or malformed id, an
            unknown key, or a field the run specification or the expert
            configuration rejects.
    """
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SuiteError(f"{path}: expected a mapping with version: 1")
    defaults = data.get("defaults") or {}
    base_budget = _budget(defaults.get("budget") or {}, "defaults")
    tasks = []
    seen: set[str] = set()
    for entry in data.get("tasks") or []:
        task = _task(entry, base_budget)
        if task.id in seen:
            raise SuiteError(f"duplicate task id {task.id!r}")
        seen.add(task.id)
        tasks.append(task)
    if not tasks:
        raise SuiteError(f"{path}: no tasks")
    return tuple(tasks)


def _codesign(raw: object, task_id: str, core: str, mesh: tuple[int, int]) -> CodesignConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - _CODESIGN_KEYS:
        raise SuiteError(f"task {task_id}: codesign takes only {sorted(_CODESIGN_KEYS)}")
    space_raw = raw.get("space") or {}
    sizes, assocs = {}, {}
    for name, choice in space_raw.items():
        if name == "networks":
            continue
        if not isinstance(choice, dict) or set(choice) != {"sizes", "assocs"}:
            raise SuiteError(f"task {task_id}: codesign space {name!r} needs sizes and assocs")
        sizes[name] = tuple(choice["sizes"])
        assocs[name] = tuple(choice["assocs"])
    try:
        space = DesignSpace(
            sizes=sizes, assocs=assocs, networks=tuple(space_raw.get("networks") or (DEFAULT_NETWORK,))
        )
        config = CodesignConfig(
            space=space,
            grid={knob: tuple(values) for knob, values in (raw.get("grid") or {}).items()},
            simulations=raw.get("simulations", 20),
            batch=raw.get("batch", 2),
            area_budget_ratio=raw.get("area_budget_ratio", 1.0),
        )
        grid = config.grid_designs()
        # The area model needs a whole number of sets; check every geometry
        # now, so a search never meets one it cannot price.
        for name in space.sizes:
            for size in space.sizes[name]:
                for assoc in space.assocs[name]:
                    cache_arrays(name, size, assoc)
    except ValueError as e:
        raise SuiteError(f"task {task_id}: codesign: {e}") from e
    if "xbar_config" in space.networks and mesh[1] != 1:
        raise SuiteError(
            f"task {task_id}: codesign network xbar_config needs a mesh with one row of tiles, "
            f"since OpenPiton's crossbar has one port per column; this mesh has {mesh[1]} rows"
        )
    for value, what in ((config.simulations, "simulations"), (config.batch, "batch")):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SuiteError(f"task {task_id}: codesign {what} must be a positive int, got {value!r}")
    ratio = config.area_budget_ratio
    if ratio is not None and (not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or ratio <= 0):
        raise SuiteError(f"task {task_id}: codesign area_budget_ratio must be positive or null, got {ratio!r}")
    if not config.grid:
        raise SuiteError(f"task {task_id}: codesign needs a grid for the grid search")
    if not all(space.contains(d) for d in grid):
        raise SuiteError(f"task {task_id}: codesign grid holds designs outside its space")
    for design in grid:
        broken = design.way_violations(core)
        if broken:
            raise SuiteError(f"task {task_id}: codesign grid holds {design.describe()}, whose {broken[0]}")
    if len(grid) > config.simulations:
        raise SuiteError(f"task {task_id}: codesign grid has {len(grid)} designs, more than its {config.simulations} simulations")
    return config


def _budget(raw: dict, where: str, base: Budget | None = None) -> Budget:
    base = base or Budget()
    unknown = set(raw) - {"max_iterations", "max_usd", "max_wall_s"}
    if unknown:
        raise SuiteError(f"{where}: unknown budget keys {sorted(unknown)}")
    try:
        return Budget(
            max_iterations=raw.get("max_iterations", base.max_iterations),
            max_usd=raw.get("max_usd", base.max_usd),
            max_wall_s=raw.get("max_wall_s", base.max_wall_s),
        )
    except ValueError as e:
        raise SuiteError(f"{where}: {e}") from e


def _workload_args(raw: object, task_id: str) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """A task's ``workload_args`` mapping, program -> {finish_mask, run_args},
    as MaceSpec.workload_args entries; MaceSpec checks the values."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise SuiteError(f"task {task_id}: workload_args must map each program to finish_mask and run_args")
    entries = []
    for program, options in raw.items():
        if not isinstance(options, dict) or set(options) - {"finish_mask", "run_args"}:
            raise SuiteError(f"task {task_id}: workload_args for {program!r} takes only finish_mask and run_args")
        run_args = options.get("run_args") or []
        if not isinstance(run_args, list):
            raise SuiteError(f"task {task_id}: run_args for {program!r} must be a list")
        entries.append((program, str(options.get("finish_mask", "")), tuple(run_args)))
    return tuple(entries)


def _task(entry: object, base_budget: Budget) -> SuiteTask:
    if not isinstance(entry, dict):
        raise SuiteError(f"each task must be a mapping, got {entry!r}")
    task_id = entry.get("id")
    if not isinstance(task_id, str) or not _ID_RE.match(task_id):
        raise SuiteError(f"task id must be lowercase letters, digits, '.', '_', or '-', got {task_id!r}")
    unknown = set(entry) - _TASK_KEYS
    if unknown:
        raise SuiteError(f"task {task_id}: unknown keys {sorted(unknown)}")
    mesh = entry.get("mesh")
    if not (isinstance(mesh, list) and len(mesh) == 2):
        raise SuiteError(f"task {task_id}: mesh must be [x_tiles, y_tiles], got {mesh!r}")
    workloads = entry.get("workloads")
    if not (isinstance(workloads, list) and workloads):
        raise SuiteError(f"task {task_id}: workloads must be a non-empty list")
    expert = entry.get("expert") or {}
    if not isinstance(expert, dict) or set(expert) - {"caches", "config_rtl"}:
        raise SuiteError(f"task {task_id}: expert takes only caches and config_rtl")
    caches = {}
    for name, geometry in (expert.get("caches") or {}).items():
        if name not in DEFAULT_CACHES or not (isinstance(geometry, list) and len(geometry) == 2):
            raise SuiteError(f"task {task_id}: expert cache {name!r} must be one of {sorted(DEFAULT_CACHES)} as [size, assoc]")
        caches[name] = (geometry[0], geometry[1])
    try:
        task = SuiteTask(
            id=task_id,
            core=entry.get("core", "ariane"),
            mesh=(mesh[0], mesh[1]),
            workloads=tuple(workloads),
            objective=entry.get("objective", ""),
            budget=_budget(entry.get("budget") or {}, f"task {task_id}", base_budget),
            verified=bool(entry.get("verified", False)),
            rtl_timeout=entry.get("rtl_timeout"),
            max_cycle=entry.get("max_cycle"),
            expert_caches=caches,
            expert_config_rtl=tuple(expert.get("config_rtl") or ()),
            codesign=_codesign(entry.get("codesign"), task_id, entry.get("core", "ariane"), (mesh[0], mesh[1])),
            workload_args=_workload_args(entry.get("workload_args"), task_id),
        )
        spec = task.spec()
        expert_cfg = task.expert_task()
        PitonConfig(
            core=spec.core, x_tiles=spec.target_mesh[0], y_tiles=spec.target_mesh[1],
            caches={**DEFAULT_CACHES, **caches},
            config_rtl=tuple(sorted(set(PitonConfig().config_rtl) | set(expert_cfg.config_rtl or ()))),
        )
    except (TypeError, ValueError) as e:
        raise SuiteError(f"task {task_id}: {e}") from e
    return task
