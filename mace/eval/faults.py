"""mace.eval.faults -- seeded faults that break a run's first plan.

A seeded fault changes the first plan's ``config`` and ``workload`` tasks
before any of them runs; later plans are left alone, so a run measures
whether the loop recovers from a failure it did not cause. The batch
runner's ``seeded_<name>`` methods apply one each (see
:mod:`mace.eval.runner`), and ``examples/recovery_seeded.py`` runs them
one task at a time.

``verified`` marks a fault seen to break every run it was seeded into, on
the cores it lists. A candidate stays unverified until a pilot shows it
breaks the build or the simulation.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable

from mace.spec import Task

FPGA_DEFINE = "PITON_FPGA_SYNTH"
BIST_DEFINE = "CONFIG_DISABLE_BIST_CLEAR"


@dataclass(frozen=True)
class Fault:
    name: str
    description: str
    cores: frozenset[str]
    verified: bool
    change: Callable[[Task], Task]

    def break_task(self, task: Task) -> Task:
        """*task* with the fault applied; ``unit_test`` tasks are left alone."""
        if task.kind not in ("config", "workload"):
            return task
        return self.change(task)


def _with_defines(task: Task, add: set[str] = frozenset(), drop: set[str] = frozenset()) -> Task:
    flags = (set(task.config_rtl or ()) | set(add)) - set(drop)
    return dataclasses.replace(task, config_rtl=tuple(sorted(flags)) or None)


def _with_cache(task: Task, name: str, geometry: tuple[int, int]) -> Task:
    caches = dict(task.caches or ())
    caches[name] = geometry
    return dataclasses.replace(task, caches=tuple(sorted(caches.items())))


FAULTS: dict[str, Fault] = {
    fault.name: fault
    for fault in (
        Fault(
            "fpga_synth",
            f"Adds {FPGA_DEFINE}, a define for FPGA synthesis; the Verilator build fails with %Error-PINNOTFOUND.",
            frozenset(("ariane", "pico")),
            True,
            lambda task: _with_defines(task, add={FPGA_DEFINE}),
        ),
        Fault(
            "drop_bist",
            f"Removes {BIST_DEFINE}; a PicoRV32 mesh builds and its simulation times out.",
            frozenset(("pico",)),
            True,
            lambda task: _with_defines(task, drop={BIST_DEFINE}),
        ),
        Fault(
            "l1d_three_way",
            "Sets a three-way L1D, an associativity that is not a power of two.",
            frozenset(("ariane",)),
            False,
            lambda task: _with_cache(task, "l1d", (6144, 3)),
        ),
        Fault(
            "l15_below_l1d",
            "Makes the L1.5 smaller than the L1D it backs.",
            frozenset(("ariane",)),
            False,
            lambda task: _with_cache(task, "l15", (2048, 4)),
        ),
    )
}


def first_plan_breaker(fault: Fault) -> Callable[[int, tuple[Task, ...]], tuple[Task, ...]]:
    """A ``plan_hook`` for :func:`mace.orchestrator.run_mace_loop` that
    applies *fault* to iteration 0's plan and leaves later plans alone."""

    def hook(iteration: int, tasks: tuple[Task, ...]) -> tuple[Task, ...]:
        if iteration != 0:
            return tasks
        return tuple(fault.break_task(t) for t in tasks)

    return hook
