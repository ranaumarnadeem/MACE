"""Turn evaluation run databases into Markdown tables and a per-run CSV.

Reads every run that examples/eval_batch.py labelled, from one database or
several (one per VM), and writes:

- one row per task and method: passes out of runs, median time to pass,
  median machine time, builds, simulations, tokens, and dollars;
- one row per method across tasks;
- each method compared with MACE task by task (Wilcoxon signed-rank test
  on time to pass);
- for co-design searches, one row per task and method: the best feasible
  finish time found, the simulations needed to come within 5% of the best
  any search found, and the proposals rejected before a build;
- with --csv, every run's numbers, for plots.

A run that a later run of the same task, method, and repeat replaced, such
as one a stopped batch left running, stays in the CSV, marked replaced, and
the tables leave it out.

Reads never start Ray.

Run:
    python examples/eval_report.py runs/eval.db
    python examples/eval_report.py vm1.db vm2.db --out runs/eval.md --csv runs/eval.csv
"""

from __future__ import annotations

import argparse
import csv

from mace.eval.report import (
    codesign_rows,
    codesign_summary,
    markdown_table,
    method_totals,
    paired_test,
    rows_from,
    summarize,
)
from mace.metrics import DBReader

TASK_COLUMNS = [
    "task", "method", "runs", "passed", "time_to_pass_s", "time_to_pass_iqr_s", "machine_s",
    "builds", "simulations", "llm_calls", "thinking_tokens", "usd", "false_accepts",
]
METHOD_COLUMNS = ["method", "tasks", "runs", "passed", "time_to_pass_s", "machine_s", "usd_total"]
TEST_COLUMNS = ["method", "reference", "tasks", "median_difference", "w_plus", "p"]
CODESIGN_COLUMNS = [
    "task", "method", "searches", "best_sim_time", "best_known", "reached", "sims_to_near_best", "feasible_share",
    "rejected",
]


def codesign_rows_from(paths: list[str]) -> list[dict]:
    rows = []
    for path in paths:
        reader = DBReader(path)
        try:
            rows.extend(codesign_rows(reader))
        finally:
            reader.close()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("databases", nargs="+")
    ap.add_argument("--out", default=None, help="Write the Markdown here as well as to stdout")
    ap.add_argument("--csv", default=None, help="Write one row per run here")
    args = ap.parse_args()

    all_rows = rows_from(args.databases)
    if not all_rows:
        print("no labelled evaluation runs in", ", ".join(args.databases))
        return 1
    rows = [r for r in all_rows if not r["method"].startswith("codesign_")]
    sections = []
    if rows:
        methods = sorted({r["method"] for r in rows} - {"mace"})
        tests = [paired_test(rows, m) for m in methods] if any(r["method"] == "mace" for r in rows) else []
        sections += [
            "## Per task and method\n\n" + markdown_table(summarize(rows), TASK_COLUMNS),
            "## Per method\n\n" + markdown_table(method_totals(rows), METHOD_COLUMNS),
            "## Against MACE, time to pass\n\n" + (markdown_table(tests, TEST_COLUMNS) if tests else "No MACE runs."),
        ]
    searches = codesign_rows_from(args.databases)
    if searches:
        sections.append("## Co-design searches\n\n" + markdown_table(codesign_summary(searches), CODESIGN_COLUMNS))
    replaced = sum(1 for r in all_rows if r["replaced"])
    if replaced:
        sections.append(f"Runs left out because a later run of the same task, method, and repeat replaced them: {replaced}.")
    text = "\n\n".join(sections)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_rows[0]))
            writer.writeheader()
            writer.writerows(all_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
