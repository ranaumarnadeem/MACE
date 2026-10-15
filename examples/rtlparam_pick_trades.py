"""Pick the trade designs to simulate after an RTL-parameter sweep.

Reads the latest finished ``rtlparam_sweep`` run of a task from a metrics
database, takes each passing single change's effect on the finish time, and
prints the in-budget combinations with the lowest predicted finish time as a
``trades:`` list to paste into the task's ``rtlparam`` block, or, with
``--write-suite``, writes a copy of the suite with the trades already set on the
task. ``rtlparam_trades`` then simulates them. The prediction sums the single
effects; it picks designs and is not a result.

    python examples/rtlparam_pick_trades.py --task ariane-4x4-matmul-scaled \\
        --db runs/rtlparam.db [--suite examples/eval/tasks_rtlparam.yaml] [--k 5] [--max-changes 3] \\
        [--write-suite runs/tasks_rtlparam_trades.yaml]
"""

from __future__ import annotations

import argparse
import copy
import os
import sys

import yaml

from mace.codesign.trades import format_trades, pick_trades, sweep_effects
from mace.eval.suite import load_suite
from mace.metrics import DBReader

DEFAULT_SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval", "tasks_rtlparam.yaml")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--suite", default=DEFAULT_SUITE)
    ap.add_argument("--k", type=int, default=5, help="How many trades to print")
    ap.add_argument("--max-changes", type=int, default=3, help="Most single changes in one trade")
    ap.add_argument("--write-suite", metavar="PATH", help="Also write a copy of the suite with the trades set on the task")
    args = ap.parse_args()

    tasks = {t.id: t for t in load_suite(args.suite)}
    if args.task not in tasks or tasks[args.task].rtlparam is None:
        ap.error(f"{args.task} is not an RTL-parameter task in {args.suite}")
    config = tasks[args.task].rtlparam

    db = DBReader(args.db)
    try:
        runs = db.query(
            "SELECT run_id FROM runs WHERE method = 'rtlparam_sweep' AND task = ? AND status IN ('passed', 'budget_exceeded') "
            "ORDER BY started_at DESC LIMIT 1",
            (args.task,),
        )
        if not runs:
            print(f"no finished rtlparam_sweep run of {args.task} in {args.db}", file=sys.stderr)
            return 1
        rows = db.query(
            "SELECT design, passed, sim_time, simulated, hart_cycles, storage_bits FROM evaluations "
            "WHERE run_id = ? ORDER BY idx",
            (runs[0]["run_id"],),
        )
    finally:
        db.close()

    tiles = tasks[args.task].mesh[0] * tasks[args.task].mesh[1]
    default_time, effects = sweep_effects(rows, config.finish, tiles)
    if default_time is None:
        print("the sweep's default design has no finish time, so there is nothing to compare with", file=sys.stderr)
        return 1
    print(f"# sweep run {runs[0]['run_id']}: {len(effects)} passing single changes; finish time is {config.finish}")
    for e in sorted(effects, key=lambda e: e.delta):
        print(f"#   {e.name}={e.value}: {e.delta:+d} ({100 * e.delta / default_time:+.2f}%), {e.bits:+d} storage bits")
    trades = pick_trades(effects, config.space, config.storage_budget_bits(), args.k, args.max_changes)
    if not trades:
        print("# no in-budget combination is predicted to beat the default design")
        return 0
    print(format_trades(trades, default_time, config.storage_budget_bits(), config.storage_budget_ratio))
    if args.write_suite:
        write_suite(args.suite, args.task, trades, args.write_suite)
        load_suite(args.write_suite)  # the loader refuses a trade that is not in budget
        print(f"# wrote {args.write_suite}: {args.suite} with these trades set on {args.task}")
    return 0


def write_suite(suite: str, task: str, trades, out: str) -> None:
    """Write a copy of *suite* in which *task* lists *trades*. The other tasks are unchanged; comments are not kept."""
    with open(suite, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    for entry in raw["tasks"]:
        if entry["id"] == task:
            # Tasks that share the rtlparam block through a YAML alias share one object; copy it before changing it.
            block = copy.deepcopy(entry["rtlparam"])
            block["trades"] = [dict(t.changes) for t in trades]
            entry["rtlparam"] = block
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False)


if __name__ == "__main__":
    raise SystemExit(main())
