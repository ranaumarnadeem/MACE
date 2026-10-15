"""mace.codesign.verdict -- the finish time of a design, and the go/no-go rule.

An RTL-parameter search compares designs by a finish time. The testbench's
``sim_time`` includes the cycles the harts spend printing their counters to the
fake UART, one after another, and the length of that tail depends on the
design: in the 2x2 sweep, ``home_alloc=MIDDLE`` shortened ``sim_time`` by 2%
while the harts finished 3.6% later. So the default finish time is the compute
time, the latest hart's end minus the earliest hart's start, from the per-hart
``mcycle`` values. ``sim_time`` stays recorded.

The Oct 30 rule: go only if at least one in-budget design beats the default's
finish time by the margin in a run in which every tile passed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mace.codesign.rtl import RtlDesign

COMPUTE = "compute_cycles"
SIM_TIME = "sim_time"
FINISH_KINDS = (COMPUTE, SIM_TIME)


def compute_cycles(hart_cycles: dict, tiles: int) -> int | None:
    """The compute time of a run: for each program that printed per-hart
    cycles, the latest hart's end minus the earliest hart's start, summed.

    *hart_cycles* maps each program to ``{hart: (start, end)}``; the harts may
    be ints or JSON strings. ``None`` when no program printed, or when one
    printed fewer lines than there are *tiles*, since a missing hart could be
    the slowest.
    """
    total, seen = 0, False
    for by_hart in hart_cycles.values():
        if len(by_hart) != tiles:
            return None
        starts = [value[0] for value in by_hart.values()]
        ends = [value[1] for value in by_hart.values()]
        total += max(ends) - min(starts)
        seen = True
    return total if seen else None


def row_finish(row: dict, finish: str, tiles: int) -> int | None:
    """The finish time of an ``evaluations`` row under *finish*, or ``None``
    for a row that was not simulated, did not pass, or lacks the data."""
    if finish not in FINISH_KINDS:
        raise ValueError(f"finish must be one of {FINISH_KINDS}, got {finish!r}")
    if not row["simulated"] or not row["passed"] or row["sim_time"] is None:
        return None
    if finish == SIM_TIME:
        return row["sim_time"]
    return compute_cycles(json.loads(row["hart_cycles"] or "{}"), tiles)


def row_changes(row: dict) -> tuple[tuple[str, int | str], ...]:
    """The parameters of an ``evaluations`` row's design that are not at their defaults, sorted."""
    values = json.loads(row["design"])["rtl"]
    return tuple(sorted(RtlDesign.of(values, tuple(values)).changed().items()))


@dataclass(frozen=True)
class Candidate:
    """An in-budget design that passed, with its finish time against the default's."""

    changes: tuple[tuple[str, int | str], ...]
    finish: int
    gain: float  # (default - finish) / default; positive is faster
    storage_bits: int


@dataclass(frozen=True)
class Verdict:
    default_finish: int | None
    # Every in-budget design that passed, best first.
    candidates: tuple[Candidate, ...]
    margin: float
    # False when runs of the default design disagree; the simulation is meant to be deterministic.
    default_consistent: bool = True

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def go(self) -> bool:
        """At least one in-budget design beats the default by the margin."""
        return self.best is not None and self.best.gain >= self.margin


def assess(rows: list[dict], budget_bits: int | None, margin: float, finish: str, tiles: int) -> Verdict:
    """The go/no-go verdict from the ``evaluations`` rows of a task's runs.

    *rows* hold ``design`` (JSON), ``passed``, ``simulated``, ``sim_time``,
    ``hart_cycles`` and ``storage_bits``. A design counts when it is not the
    default, passed on every tile, and added no more than *budget_bits*. A
    design several runs simulated counts once.
    """
    entries = []
    for row in rows:
        value = row_finish(row, finish, tiles)
        if value is not None:
            entries.append((row_changes(row), value, row["storage_bits"]))
    defaults = [value for changes, value, _ in entries if not changes]
    if not defaults:
        return Verdict(default_finish=None, candidates=(), margin=margin)
    base = defaults[0]
    found: dict[tuple, Candidate] = {}
    for changes, value, bits in entries:
        if not changes or (budget_bits is not None and bits > budget_bits):
            continue
        found[changes] = Candidate(changes=changes, finish=value, gain=(base - value) / base, storage_bits=bits)
    ranked = sorted(found.values(), key=lambda c: (-c.gain, c.storage_bits, c.changes))
    return Verdict(
        default_finish=base, candidates=tuple(ranked), margin=margin, default_consistent=len(set(defaults)) == 1
    )
