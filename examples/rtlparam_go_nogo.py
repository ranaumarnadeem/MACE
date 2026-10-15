"""The go/no-go verdict for an RTL-parameter task.

Reads every ``rtlparam_*`` run of a task from one or more metrics databases and
applies the rule: go only if at least one in-budget design beats the default
design's finish time by the task's margin in a run in which every tile passed.
The finish time, the storage budget and the margin come from the task's
``rtlparam`` block (see mace.codesign.verdict). The script prints every in-budget
design that passed, best first, and the verdict.

    python examples/rtlparam_go_nogo.py --task ariane-4x4-matmul-scaled \\
        --db runs/rtlparam.db [--db more.db] [--suite examples/eval/tasks_rtlparam.yaml]
"""

from __future__ import annotations

import argparse
import os
import sys

from mace.codesign.verdict import assess
from mace.eval.suite import load_suite
from mace.metrics import DBReader

DEFAULT_SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval", "tasks_rtlparam.yaml")

FALLBACK = "The default parameters are balanced for this workload."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", required=True)
    ap.add_argument("--db", action="append", required=True, help="A metrics database; repeatable")
    ap.add_argument("--suite", default=DEFAULT_SUITE)
    ap.add_argument("--top", type=int, default=10, help="How many designs to list")
    args = ap.parse_args()

    tasks = {t.id: t for t in load_suite(args.suite)}
    if args.task not in tasks or tasks[args.task].rtlparam is None:
        ap.error(f"{args.task} is not an RTL-parameter task in {args.suite}")
    task = tasks[args.task]
    config = task.rtlparam
    tiles = task.mesh[0] * task.mesh[1]
    budget = config.storage_budget_bits()

    rows: list[dict] = []
    for path in args.db:
        db = DBReader(path)
        try:
            rows += db.query(
                "SELECT e.design, e.passed, e.simulated, e.sim_time, e.hart_cycles, e.storage_bits "
                "FROM evaluations e JOIN runs r ON r.run_id = e.run_id "
                "WHERE r.task = ? AND r.method LIKE 'rtlparam_%'",
                (args.task,),
            )
        finally:
            db.close()

    verdict = assess(rows, budget, config.win_margin, config.finish, tiles)
    budget_text = "no budget" if budget is None else f"a budget of {budget} storage bits (ratio {config.storage_budget_ratio})"
    print(f"{args.task}: finish time is {config.finish}; {budget_text}; margin {100 * config.win_margin:.2f}%")
    if verdict.default_finish is None:
        print("NO VERDICT: no run has a passing default design with a finish time", file=sys.stderr)
        return 1
    print(f"default finish time {verdict.default_finish}")
    if not verdict.candidates:
        print("NO VERDICT: no design other than the default has passed within the budget yet", file=sys.stderr)
        return 1
    if not verdict.default_consistent:
        print("WARNING: runs of the default design disagree on its finish time; the simulation should be deterministic")
    print(f"{'gain':>8}  {'finish':>10}  {'bits':>6}  design")
    for c in verdict.candidates[: args.top]:
        kind = "single" if len(c.changes) == 1 else "trade"
        mark = "*" if c.gain >= config.win_margin else " "
        changes = ", ".join(f"{n}={v}" for n, v in c.changes)
        print(f"{100 * c.gain:>+7.2f}%{mark} {c.finish:>10}  {c.storage_bits:>+6d}  {changes}  ({kind})")
    print(f"{len(verdict.candidates)} in-budget designs passed on every tile; * marks a gain of at least the margin")
    if verdict.go:
        best = verdict.best
        changes = ", ".join(f"{n}={v}" for n, v in best.changes)
        print(f"GO: {changes} beats the default by {100 * best.gain:.2f}%, within the budget.")
    else:
        best = verdict.best
        near = f" The best in-budget design is {100 * best.gain:+.2f}% against it." if best is not None else ""
        print(f"NO-GO: no in-budget design beats the default by {100 * config.win_margin:.2f}%.{near} {FALLBACK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
