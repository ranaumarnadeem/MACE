"""mace.codesign.trades -- pick in-budget trade designs from a sweep's results.

``rtlparam_sweep`` simulates the default design and then each parameter alone
at each of its other values. At a storage budget of the default design's own
storage, a single increase never fits: a larger buffer must be paid for with a
smaller one. So the sweep cannot show an in-budget gain by itself. This module
reads a sweep's evaluations, takes each passing single change's effect on the
finish time, and ranks the combinations that fit the budget by the sum of
their effects.

The sum is a prediction that the effects add. It only chooses which designs to
simulate; ``rtlparam_trades`` simulates them, and only a simulation says
whether a trade beats the default.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from mace.codesign.rtl import RtlDesign, RtlSpace
from mace.codesign.verdict import row_changes, row_finish


@dataclass(frozen=True)
class Effect:
    """One parameter at one non-default value, alone, in a passing simulation."""

    name: str
    value: int | str
    # Finish time against the default design; negative when the change helps.
    delta: int
    # Storage bits per tile against the default design.
    bits: int


@dataclass(frozen=True)
class Trade:
    """A design built from single changes, with the sweep's prediction for it."""

    design: RtlDesign
    changes: tuple[tuple[str, int | str], ...]
    predicted_delta: int
    storage_delta: int


def sweep_effects(rows: list[dict], finish: str, tiles: int) -> tuple[int | None, list[Effect]]:
    """The default design's finish time and the effect of each passing single
    change, from a sweep's ``evaluations`` rows (see
    :func:`mace.codesign.verdict.row_finish` for the finish times *finish*
    names). A design that failed, was rejected, lacks the data, or changes
    more than one parameter gives no effect; a sweep with no passing default
    gives ``(None, [])``."""
    entries = []
    for row in rows:
        value = row_finish(row, finish, tiles)
        if value is not None:
            entries.append((row_changes(row), value, row["storage_bits"]))
    default_time = next((value for changes, value, _ in entries if not changes), None)
    if default_time is None:
        return None, []
    effects = []
    for changes, value, bits in entries:
        if len(changes) == 1:
            ((name, setting),) = changes
            effects.append(Effect(name, setting, value - default_time, bits))
    return default_time, effects


def pick_trades(
    effects: list[Effect],
    space: RtlSpace,
    budget_bits: int | None,
    k: int = 5,
    max_changes: int = 3,
) -> list[Trade]:
    """The *k* designs of up to *max_changes* single changes (at most one per
    parameter) that fit *budget_bits* and have the lowest predicted finish
    time, which is the sum of their effects; a design predicted no faster than
    the default is left out. Ties go to the design that adds less storage.

    A design of one change is possible when that change costs nothing within
    the budget, as a change of the home slice does.
    """
    by_param: dict[str, list[Effect]] = {}
    for effect in effects:
        if effect.name in space.values and effect.value in space.values[effect.name]:
            by_param.setdefault(effect.name, []).append(effect)
    trades: dict[RtlDesign, Trade] = {}
    for size in range(1, max_changes + 1):
        for names in itertools.combinations(sorted(by_param), size):
            for chosen in itertools.product(*(by_param[n] for n in names)):
                bits = sum(e.bits for e in chosen)
                predicted = sum(e.delta for e in chosen)
                if predicted >= 0 or (budget_bits is not None and bits > budget_bits):
                    continue
                design = space.design({e.name: e.value for e in chosen})
                trades[design] = Trade(
                    design=design,
                    changes=tuple((e.name, e.value) for e in chosen),
                    predicted_delta=predicted,
                    storage_delta=bits,
                )
    ranked = sorted(trades.values(), key=lambda t: (t.predicted_delta, t.storage_delta, t.changes))
    return ranked[:k]


def format_trades(
    trades: list[Trade], default_time: int, budget_bits: int | None, ratio: float | None
) -> str:
    """The trades as the ``trades:`` list of a suite task's ``rtlparam`` block,
    each with its prediction as a comment."""
    budget = "no budget" if budget_bits is None else f"a budget of {budget_bits} bits (ratio {ratio})"
    lines = [
        f"# default finish time {default_time}; {budget}.",
        "# Each prediction is the sum of the single effects in the sweep. It picks what to simulate and is not a result.",
        "trades:",
    ]
    for trade in trades:
        entry = ", ".join(f"{n}: {v}" for n, v in trade.changes)
        percent = 100 * trade.predicted_delta / default_time
        lines.append(
            f"  - {{{entry}}}  # predicted {trade.predicted_delta:+d} ({percent:+.1f}%), "
            f"storage {trade.storage_delta:+d} bits"
        )
    return "\n".join(lines)
