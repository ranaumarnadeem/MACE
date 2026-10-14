"""mace.eval.faults -- seeded faults that break a run's first plan.

A seeded fault changes the first plan's ``config`` and ``workload`` tasks
before any of them runs; later plans are left alone, so a run measures
whether the loop recovers from a failure it did not cause. The batch
runner's ``seeded_<name>`` methods apply one each (see
:mod:`mace.eval.runner`), and ``examples/recovery_seeded.py`` runs them
one task at a time. Its ``seeded_retry_<name>`` methods apply the same
fault to the retry agent's first design instead. A fault applies to tasks
on the cores it lists, on a mesh with at least ``min_rows`` rows of tiles.

``verified`` marks a fault seen to break every run it was seeded into, on
the cores it lists. A candidate stays unverified until a pilot shows it
breaks the build or the simulation; the runner's ``faultcheck_<name>``
methods are that pilot, the expert configuration with the fault applied.

``held_out`` marks a fault defined after the loop's recovery checks were
designed, of a kind the first four do not cover. Evaluations report the
two groups apart.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable

from mace.spec import Task

FPGA_DEFINE = "PITON_FPGA_SYNTH"
BIST_DEFINE = "CONFIG_DISABLE_BIST_CLEAR"
NO_MEMORY_DEFINE = "PITONSYS_NO_MC"
MONITORING_DEFINE = "MINIMAL_MONITORING"


@dataclass(frozen=True)
class Fault:
    name: str
    description: str
    cores: frozenset[str]
    verified: bool
    change: Callable[[Task], Task]
    # The fewest rows of tiles a mesh needs for the fault to break it.
    min_rows: int = 1
    held_out: bool = False
    # For a fault of two changes: the single faults it applies together.
    parts: tuple[str, ...] = ()

    def applies_to(self, core: str, mesh: tuple[int, int]) -> bool:
        """Whether seeding this fault into a task on *core* and *mesh* breaks it."""
        return core in self.cores and mesh[1] >= self.min_rows

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


def _combined(*names: str) -> Callable[[Task], Task]:
    """The change that applies each named single fault in turn; they are looked up when it runs."""

    def change(task: Task) -> Task:
        for name in names:
            task = FAULTS[name].change(task)
        return task

    return change


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
            "crossbar",
            "Selects OpenPiton's crossbar, which has one port per column of tiles; on a mesh with two or more "
            "rows the Verilator build fails with duplicate pin connections.",
            frozenset(("ariane", "pico")),
            True,
            lambda task: dataclasses.replace(task, network="xbar_config"),
            min_rows=2,
        ),
        Fault(
            "l1d_eight_way",
            "Sets an 8 KB eight-way L1D, more ways than the four-way L1.5; build() refuses it under Ariane's way "
            "rule, without a build.",
            frozenset(("ariane",)),
            True,
            lambda task: _with_cache(task, "l1d", (8192, 8)),
        ),
        # Held out: defined on 2026-10-12, of kinds the four faults above do not cover, and checked by
        # faultcheck_<name> on ariane-2x2-barrier and pico-2x2-addi (laptop, commit 8495669, one run each).
        # `cores` names the cores whose run failed.
        Fault(
            "l15_eight_way",
            "Sets an 8 KB eight-way L1.5. Ariane's way rule allows it and the build passes; the Ariane "
            "simulation times out, and a PicoRV32 mesh passes.",
            frozenset(("ariane",)),
            True,
            lambda task: _with_cache(task, "l15", (8192, 8)),
            held_out=True,
        ),
        Fault(
            "no_mem_ctrl",
            f"Adds {NO_MEMORY_DEFINE}, which leaves out the chipset's memory controller; the build passes "
            "and the simulation times out.",
            frozenset(("ariane", "pico")),
            True,
            lambda task: _with_defines(task, add={NO_MEMORY_DEFINE}),
            held_out=True,
        ),
        Fault(
            "l2_direct_mapped",
            "Sets a direct-mapped L2; the Verilator build fails with a zero replication value and a missing "
            "module, l2_priority_encoder_0.",
            frozenset(("ariane", "pico")),
            True,
            lambda task: _with_cache(task, "l2", (65536, 1)),
            held_out=True,
        ),
        Fault(
            "drop_monitoring",
            f"Removes the default {MONITORING_DEFINE}, which turns on the testbench's full monitor block; a "
            "PicoRV32 simulation ends with no pass status, and an Ariane one passes.",
            frozenset(("pico",)),
            True,
            lambda task: dataclasses.replace(
                task, drop_rtl=tuple(sorted({*(task.drop_rtl or ()), MONITORING_DEFINE}))
            ),
            held_out=True,
        ),
        # Two changes at once, each a fault above that breaks the design alone: undoing either one still
        # leaves it broken, so the failure shows one cause at a time. Unverified until faultcheck_<name>.
        Fault(
            "synth_and_no_mem",
            f"Adds {FPGA_DEFINE} and {NO_MEMORY_DEFINE}: the build fails on the first, and once that is undone "
            "the simulation times out on the second.",
            frozenset(("ariane", "pico")),
            False,
            _combined("fpga_synth", "no_mem_ctrl"),
            parts=("fpga_synth", "no_mem_ctrl"),
        ),
        Fault(
            "bist_and_synth",
            f"Removes {BIST_DEFINE} and adds {FPGA_DEFINE}: the build fails on the second, and once that is "
            "undone the PicoRV32 simulation times out on the first.",
            frozenset(("pico",)),
            False,
            _combined("drop_bist", "fpga_synth"),
            parts=("drop_bist", "fpga_synth"),
        ),
        Fault(
            "l2_and_no_mem",
            f"Sets a direct-mapped L2 and adds {NO_MEMORY_DEFINE}: the build fails on the first, and once that "
            "is undone the simulation times out on the second.",
            frozenset(("ariane", "pico")),
            False,
            _combined("l2_direct_mapped", "no_mem_ctrl"),
            parts=("l2_direct_mapped", "no_mem_ctrl"),
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


def first_design_breaker(fault: Fault) -> Callable[[int, Task], Task]:
    """A ``design_hook`` for :func:`mace.baselines.retry_agent.run_retry_agent`
    that applies *fault* to attempt 0's design and leaves later designs alone."""

    def hook(attempt: int, design: Task) -> Task:
        return fault.break_task(design) if attempt == 0 else design

    return hook
