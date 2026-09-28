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
from mace.spec import Budget, MaceSpec, Task

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_TASK_KEYS = frozenset(
    ("id", "core", "mesh", "workloads", "objective", "verified", "expert", "budget", "rtl_timeout")
)


class SuiteError(ValueError):
    """The suite file is malformed; the message names the task and field."""


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
    expert_caches: dict[str, tuple[int, int]] = field(default_factory=dict)
    expert_config_rtl: tuple[str, ...] = ()

    def spec(self) -> MaceSpec:
        """The run specification every method gets for this task."""
        return MaceSpec(
            workloads=self.workloads,
            objective=self.objective,
            core=self.core,
            target_mesh=self.mesh,
            budget=self.budget,
            rtl_timeout=self.rtl_timeout,
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
            expert_caches=caches,
            expert_config_rtl=tuple(expert.get("config_rtl") or ()),
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
