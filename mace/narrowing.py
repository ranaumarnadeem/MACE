"""mace.narrowing -- find which change to the configuration breaks a failed task.

A failed task often carries several changes from the defaults, and the
recorded runs show the loop blaming the wrong one: a planner told to edit
``manycore_top.v`` re-added the define that broke the build. The build is
deterministic, so the question has a fixed answer: rebuild the task without
each change and see which rebuild passes.

The reference is the mesh's default configuration plus the RTL defines the
objective says the build needs. A *change* is a difference between the failed
configuration and the reference, in either direction: a define the reference
lacks, a define the reference has and the task lacks, a cache whose geometry
differs, or the crossbar. A *probe* is the failed task with some changes
undone, built and checked on the gate workloads. :func:`narrow` runs the
probes through a callable the orchestrator supplies, so this module needs no
Ray and no checkout.

A probe that passes is evidence, not the run's pass: it may leave out a cache
size the objective asked for, which the define parser cannot see. The re-plan
decides what to build next with the answer in hand.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable

from chia_openpiton.state_def import DEFAULT_CACHES, PitonConfig
from mace.integrator import NOT_STARTED
from mace.loop import _config_for_task
from mace.spec import MaceSpec, StepResult, Task

ADDED_DEFINE = "added_define"
MISSING_DEFINE = "missing_define"
CACHE = "cache"
NETWORK = "network"
_KIND_ORDER = {ADDED_DEFINE: 0, MISSING_DEFINE: 1, CACHE: 2, NETWORK: 3}

# The most changes probed one at a time; a task with more goes straight to the all-undone probe.
MAX_SINGLE_PROBES = 4
# A probe cycle is at least this long when a failed task's own build and run were quick (a build error ends fast;
# the probe that follows is a full build).
MIN_CYCLE_S = 300.0

# Failures whose cause is no change in the configuration.
SKIP_REASONS = frozenset(("way_rule", NOT_STARTED))

# Verdicts. "culprit" and "combination" name a change to leave out; "not_config" means the reference fails too.
CULPRIT, COMBINATION, NOT_CONFIG, UNRESOLVED, NO_CHANGES, SKIPPED = (
    "culprit", "combination", "not_config", "unresolved", "no_changes", "skipped",
)


@dataclass(frozen=True)
class Change:
    """One difference between a configuration and the reference.

    *name* is a define, a cache (``l1i``, ``l1d``, ``l15``, ``l2``), or
    ``network``; *value* is the configuration's own geometry or interconnect
    for a cache or network change, and empty for a define.
    """

    kind: str
    name: str
    value: tuple = ()

    @property
    def sort_key(self) -> tuple:
        return (_KIND_ORDER[self.kind], self.name)

    def describe(self) -> str:
        """What the failed configuration does."""
        if self.kind == ADDED_DEFINE:
            return f"adds the RTL define {self.name}"
        if self.kind == MISSING_DEFINE:
            return f"leaves out the RTL define {self.name}"
        if self.kind == CACHE:
            size, assoc = self.value
            default_size, default_assoc = DEFAULT_CACHES[self.name]
            return f"sets {self.name} to {size},{assoc} (default {default_size},{default_assoc})"
        return f"uses the {self.value[0]} interconnect"

    def undone(self) -> str:
        """What a probe that puts this change back builds."""
        if self.kind == ADDED_DEFINE:
            return f"without the RTL define {self.name}"
        if self.kind == MISSING_DEFINE:
            return f"with the RTL define {self.name}"
        if self.kind == CACHE:
            default_size, default_assoc = DEFAULT_CACHES[self.name]
            return f"with {self.name} at {default_size},{default_assoc}"
        return "with the default interconnect"

    def remedy(self) -> str:
        """What the next plan should do about this change."""
        if self.kind == ADDED_DEFINE:
            return f"leave out the RTL define {self.name}"
        if self.kind == MISSING_DEFINE:
            return f"keep the RTL define {self.name}"
        if self.kind == CACHE:
            default_size, default_assoc = DEFAULT_CACHES[self.name]
            return f"keep {self.name} at its default {default_size},{default_assoc}"
        return "keep the default interconnect"


def reference_config(spec: MaceSpec, required: tuple[str, ...] = ()) -> PitonConfig:
    """The mesh's default configuration for *spec* plus the *required* RTL defines."""
    return _config_for_task(
        spec, Task(id="reference", deps=(), kind="config", spec="reference", config_rtl=tuple(required) or None)
    )


def config_changes(config: PitonConfig, reference: PitonConfig) -> tuple[Change, ...]:
    """What *config* changes from *reference*, in a fixed order: defines it
    adds, then defines it leaves out, then caches, then the interconnect."""
    changes = [Change(ADDED_DEFINE, d) for d in sorted(set(config.config_rtl) - set(reference.config_rtl))]
    changes += [Change(MISSING_DEFINE, d) for d in sorted(set(reference.config_rtl) - set(config.config_rtl))]
    changes += [
        Change(CACHE, name, tuple(config.caches[name]))
        for name in sorted(config.caches)
        if tuple(config.caches[name]) != tuple(reference.caches[name])
    ]
    if config.network_config != reference.network_config:
        changes.append(Change(NETWORK, "network", (config.network_config,)))
    return tuple(sorted(changes, key=lambda c: c.sort_key))


def undo(config: PitonConfig, changes: tuple[Change, ...], reference: PitonConfig) -> PitonConfig:
    """*config* with each of *changes* put back to what *reference* has."""
    defines = set(config.config_rtl)
    caches = dict(config.caches)
    network = config.network_config
    for change in changes:
        if change.kind == ADDED_DEFINE:
            defines.discard(change.name)
        elif change.kind == MISSING_DEFINE:
            defines.add(change.name)
        elif change.kind == CACHE:
            caches[change.name] = reference.caches[change.name]
        else:
            network = reference.network_config
    return dataclasses.replace(config, config_rtl=tuple(sorted(defines)), caches=caches, network_config=network)


def task_for_config(base: Task, config: PitonConfig, task_id: str) -> Task:
    """A task that builds *config*, taking its kind and instruction from *base*:
    :func:`mace.loop._config_for_task` gives back the same build flags."""
    defaults = set(PitonConfig().config_rtl)
    extra = tuple(sorted(set(config.config_rtl) - defaults))
    dropped = tuple(sorted(defaults - set(config.config_rtl)))
    return Task(
        id=task_id, deps=(), kind=base.kind, spec=base.spec,
        caches=tuple(sorted((name, tuple(geom)) for name, geom in config.caches.items())),
        config_rtl=extra or None,
        drop_rtl=dropped or None,
        network=config.network_config if config.network_config != PitonConfig().network_config else None,
    )


@dataclass(frozen=True)
class Probe:
    """The failed task rebuilt with *undone* put back; *result* is ``None``
    when the probe never started."""

    undone: tuple[Change, ...]
    task: Task
    result: StepResult | None

    @property
    def ran(self) -> bool:
        return self.result is not None and self.result.build.failure_reason != NOT_STARTED

    @property
    def passed(self) -> bool:
        return self.ran and self.result.passed

    def outcome(self) -> str:
        if not self.ran:
            return "did not run (time)"
        if self.passed:
            return "passed"
        if not self.result.build.success:
            return "the build failed"
        verdict = self.result.run.verdict if self.result.run is not None else None
        return f"the simulation ended {verdict}"


@dataclass(frozen=True)
class Narrowing:
    """What the probes showed about a failed task's changes from the reference."""

    verdict: str
    changes: tuple[Change, ...] = ()
    probes: tuple[Probe, ...] = ()
    # Sets of changes shown to break the design: each alone for a "culprit", all together for a "combination".
    culprits: tuple[tuple[Change, ...], ...] = ()

    @property
    def definitive(self) -> bool:
        """Whether the probes name what to leave out, so triage has nothing to add."""
        return self.verdict in (CULPRIT, COMBINATION)

    @property
    def singles_unrun(self) -> bool:
        """Whether a change was left untested alone because the run's time ran out."""
        return any(len(p.undone) == 1 and not p.ran for p in self.probes)

    def fix(self) -> str:
        """What to do, for a definitive verdict; "" otherwise."""
        if self.verdict == CULPRIT:
            return "; ".join(c.remedy() for undone in self.culprits for c in undone)
        if self.verdict == COMBINATION:
            return "build the default configuration instead of this combination: " + "; ".join(
                c.remedy() for c in self.culprits[0]
            )
        return ""

    def summary(self) -> str:
        """The note the re-plan and triage get, or "" when no probe ran."""
        ran = [p for p in self.probes if p.ran]
        if not ran:
            return ""
        lines = [f"- {' and '.join(c.undone() for c in p.undone)}: {p.outcome()}" for p in ran]
        if self.verdict == COMBINATION and self.singles_unrun:
            conclusion = (
                "Building with all of the changes put back passes; the run's time ran out before each change "
                "could be put back alone."
            )
        else:
            conclusion = {
                CULPRIT: "A build that passes shows the change it put back breaks the design. " + self.fix() + ".",
                COMBINATION: "No single change breaks the design, but the changes together do. " + self.fix() + ".",
                NOT_CONFIG: "The default configuration fails the same way, so none of this task's changes is the cause.",
                UNRESOLVED: "The run's time ran out before the probes could settle which change breaks the design.",
            }.get(self.verdict, "")
        head = "The failed task's configuration, rebuilt with changes put back and checked on the same gate workloads:"
        return "\n".join([head, *lines, conclusion]).rstrip()


def narrow(
    spec: MaceSpec,
    failed: StepResult,
    required: tuple[str, ...],
    run_tasks: Callable[[tuple[Task, ...]], tuple[StepResult, ...]],
    parallelism: int,
    time_left: Callable[[], float],
) -> Narrowing:
    """Probe *failed*'s changes from the reference (the defaults plus *required*).

    *run_tasks* builds and checks tasks, returning one result each in order;
    at most *parallelism* run together, and *time_left* gives the seconds
    left in the run. Probes start only while twice the failed task's own build
    and run time (at least :data:`MIN_CYCLE_S`) remains, so the re-plan that
    follows still fits.
    """
    if failed.task.kind not in ("config", "workload") or failed.edits or failed.build.failure_reason in SKIP_REASONS:
        return Narrowing(SKIPPED)
    reference = reference_config(spec, required)
    changes = config_changes(failed.build.config, reference)
    if not changes:
        return Narrowing(NO_CHANGES)

    runs = failed.runs or ((failed.run,) if failed.run is not None else ())
    cycle = max(failed.build.wall_time_s + sum(r.wall_time_s for r in runs), MIN_CYCLE_S)
    probes: list[Probe] = []

    def probe(groups: list[tuple[Change, ...]]) -> None:
        for start in range(0, len(groups), max(parallelism, 1)):
            batch = groups[start:start + max(parallelism, 1)]
            if time_left() < 2 * cycle:
                probes.extend(Probe(g, task_for_config(failed.task, failed.build.config, "x"), None) for g in batch)
                continue
            tasks = tuple(
                task_for_config(
                    failed.task, undo(failed.build.config, group, reference), f"narrow-{failed.task.id}-{len(probes) + i}"
                )
                for i, group in enumerate(batch)
            )
            probes.extend(Probe(g, t, r) for g, t, r in zip(batch, tasks, run_tasks(tasks)))

    if len(changes) <= MAX_SINGLE_PROBES:
        probe([(c,) for c in changes])
        passing = [p for p in probes if p.passed]
        if passing:
            return Narrowing(CULPRIT, changes, tuple(probes), tuple(p.undone for p in passing))
        if len(changes) == 1:
            return Narrowing(NOT_CONFIG if probes[0].ran else UNRESOLVED, changes, tuple(probes))
    probe([changes])
    last = probes[-1]
    if last.passed:
        return Narrowing(COMBINATION, changes, tuple(probes), (changes,))
    return Narrowing(NOT_CONFIG if last.ran else UNRESOLVED, changes, tuple(probes))
