"""Turn evaluation run databases into Markdown tables and a per-run CSV.

Reads every run that examples/eval_batch.py labelled, from one database or
several (one per VM), and writes:

- one row per task and method: passes out of runs, median time to pass,
  median machine time, builds, simulations, tokens, and dollars;
- one row per method across tasks;
- each method compared with MACE task by task (Wilcoxon signed-rank test
  on time to pass);
- with --csv, every run's numbers, for plots.

Reads never start Ray.

Run:
    python examples/eval_report.py runs/eval.db
    python examples/eval_report.py vm1.db vm2.db --out runs/eval.md --csv runs/eval.csv
"""

from __future__ import annotations

import argparse
import csv

from mace.eval.report import markdown_table, method_totals, paired_test, rows_from, summarize

TASK_COLUMNS = [
    "task", "method", "runs", "passed", "time_to_pass_s", "time_to_pass_iqr_s", "machine_s",
    "builds", "simulations", "llm_calls", "thinking_tokens", "usd", "false_accepts",
]
METHOD_COLUMNS = ["method", "tasks", "runs", "passed", "time_to_pass_s", "machine_s", "usd_total"]
TEST_COLUMNS = ["method", "reference", "tasks", "median_difference", "w_plus", "p"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("databases", nargs="+")
    ap.add_argument("--out", default=None, help="Write the Markdown here as well as to stdout")
    ap.add_argument("--csv", default=None, help="Write one row per run here")
    args = ap.parse_args()

    rows = rows_from(args.databases)
    if not rows:
        print("no labelled evaluation runs in", ", ".join(args.databases))
        return 1
    methods = sorted({r["method"] for r in rows} - {"mace"})
    tests = [paired_test(rows, m) for m in methods] if any(r["method"] == "mace" for r in rows) else []
    text = "\n\n".join(
        [
            "## Per task and method\n\n" + markdown_table(summarize(rows), TASK_COLUMNS),
            "## Per method\n\n" + markdown_table(method_totals(rows), METHOD_COLUMNS),
            "## Against MACE, time to pass\n\n" + (markdown_table(tests, TEST_COLUMNS) if tests else "No MACE runs."),
        ]
    )
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
