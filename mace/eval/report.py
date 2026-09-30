"""mace.eval.report -- the evaluation's tables, read from the run databases.

:func:`run_rows` turns every labelled run into one row of numbers: status,
wall time, machine time (build plus simulation), builds, simulations, LLM
calls, tokens, dollars, and, for the build-only ablation, false accepts.
:func:`summarize` groups rows by task and method, and :func:`method_totals`
by method across tasks. :func:`paired_test` compares two methods task by
task with a Wilcoxon signed-rank test.

Reads use :class:`mace.metrics.DBReader`, so a report starts no Ray. Rows
from several databases, one per VM, merge by simple concatenation. A run
that a later run of the same job replaced keeps its row, marked
``replaced``, and the summaries leave it out.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict

from mace.metrics import DBReader

NUMBERS = (
    "wall_s", "machine_s", "builds", "fresh_builds", "simulations", "llm_calls",
    "input_tokens", "output_tokens", "thinking_tokens", "usd",
)


def _replaced(runs: list[dict]) -> set[str]:
    """The ids of the runs that a later run of the same task, method, and
    repeat replaced; *runs* come in start order. The batch runner runs a job
    again when its run did not finish, such as one a stopped batch left
    ``running``, and the earlier run stays in the database."""
    latest = {(run["task"], run["method"], run["repeat"]): run["run_id"] for run in runs}
    return {run["run_id"] for run in runs if latest[(run["task"], run["method"], run["repeat"])] != run["run_id"]}


def run_rows(db) -> list[dict]:
    """One row per run that carries evaluation labels (task and repeat)."""
    runs = db.query(
        "SELECT run_id, task, method, repeat, status, started_at, finished_at, meta FROM runs "
        "WHERE task IS NOT NULL AND repeat IS NOT NULL ORDER BY started_at",
        (),
    )
    replaced = _replaced(runs)
    rows = []
    for run in runs:
        run_id = run["run_id"]
        tasks = db.query("SELECT build_s, run_s, wall_s, programs FROM tasks WHERE run_id = ?", (run_id,))
        calls = db.query_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o, "
            "COALESCE(SUM(thinking_tokens), 0) AS t, COALESCE(SUM(usd), 0) AS usd FROM llm_calls WHERE run_id = ?",
            (run_id,),
        )
        resims = db.query("SELECT task_id, passed FROM resimulations WHERE run_id = ?", (run_id,))
        rejected = {r["task_id"] for r in resims if not r["passed"]}
        simulated = {r["task_id"] for r in resims}
        meta = json.loads(run["meta"]) if run["meta"] else {}
        rows.append(
            {
                "run_id": run_id,
                "task": run["task"],
                "method": run["method"],
                "repeat": run["repeat"],
                "status": run["status"],
                "replaced": run_id in replaced,
                "passed": run["status"] == "passed",
                "wall_s": (run["finished_at"] - run["started_at"]) if run["finished_at"] else None,
                "machine_s": sum((t["wall_s"] or 0.0) for t in tasks),
                "builds": len(tasks),
                "fresh_builds": sum(1 for t in tasks if (t["build_s"] or 0.0) > 0),
                "simulations": sum(len(json.loads(t["programs"])) for t in tasks if t["programs"]),
                "llm_calls": calls["n"],
                "input_tokens": calls["i"],
                "output_tokens": calls["o"],
                "thinking_tokens": calls["t"],
                "usd": calls["usd"],
                "accepted_designs": len(simulated) if simulated else None,
                "false_accepts": len(rejected) if simulated else None,
                "model": meta.get("model"),
            }
        )
    return rows


def rows_from(paths: list[str]) -> list[dict]:
    """:func:`run_rows` over every database in *paths*, concatenated."""
    rows = []
    for path in paths:
        reader = DBReader(path)
        try:
            rows.extend(run_rows(reader))
        finally:
            reader.close()
    return rows


def _current(rows: list[dict]) -> list[dict]:
    """*rows* without the runs a later run of the same job replaced."""
    return [r for r in rows if not r.get("replaced")]


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _iqr(values):
    values = sorted(v for v in values if v is not None)
    if len(values) < 2:
        return None
    q = statistics.quantiles(values, n=4, method="inclusive")
    return q[2] - q[0]


def summarize(rows: list[dict]) -> list[dict]:
    """One row per (task, method): pass count and the medians of
    :data:`NUMBERS`. Time to pass is the median wall time of passed runs."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in _current(rows):
        groups[(row["task"], row["method"])].append(row)
    out = []
    for (task, method), group in sorted(groups.items()):
        passed = [r for r in group if r["passed"]]
        summary = {
            "task": task,
            "method": method,
            "runs": len(group),
            "passed": len(passed),
            "time_to_pass_s": _median(r["wall_s"] for r in passed),
            "time_to_pass_iqr_s": _iqr([r["wall_s"] for r in passed]),
        }
        for name in NUMBERS:
            summary[name] = _median(r[name] for r in group)
        accepts = [r for r in group if r["false_accepts"] is not None]
        summary["false_accepts"] = sum(r["false_accepts"] for r in accepts) if accepts else None
        out.append(summary)
    return out


def method_totals(rows: list[dict]) -> list[dict]:
    """One row per method across all tasks: pass count and medians."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in _current(rows):
        groups[row["method"]].append(row)
    out = []
    for method, group in sorted(groups.items()):
        passed = [r for r in group if r["passed"]]
        total = {
            "method": method,
            "tasks": len({r["task"] for r in group}),
            "runs": len(group),
            "passed": len(passed),
            "time_to_pass_s": _median(r["wall_s"] for r in passed),
            "machine_s": _median(r["machine_s"] for r in group),
            "usd_total": sum(r["usd"] for r in group),
        }
        out.append(total)
    return out


def wilcoxon_signed_rank(differences: list[float]) -> tuple[float, float]:
    """Exact two-sided Wilcoxon signed-rank test.

    Zero differences are dropped; tied magnitudes get their average rank.
    Returns ``(W_plus, p)``; with no nonzero difference, ``(0.0, 1.0)``.
    """
    diffs = [d for d in differences if d != 0]
    n = len(diffs)
    if n == 0:
        return 0.0, 1.0
    order = sorted(range(n), key=lambda i: abs(diffs[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(diffs[order[j + 1]]) == abs(diffs[order[i]]):
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    w_plus = sum(r for r, d in zip(ranks, diffs) if d > 0)
    # Doubled ranks are integers even with ties, so the null distribution
    # of the doubled rank sum can be counted exactly.
    doubled = [int(round(2 * r)) for r in ranks]
    counts = {0: 1}
    for r in doubled:
        nxt = defaultdict(int)
        for total, ways in counts.items():
            nxt[total] += ways
            nxt[total + r] += ways
        counts = nxt
    total_ways = 2 ** n
    observed = int(round(2 * w_plus))
    mean = sum(doubled) / 2
    extreme = abs(observed - mean)
    tail = sum(ways for total, ways in counts.items() if abs(total - mean) >= extreme - 1e-9)
    return w_plus, min(1.0, tail / total_ways)


def paired_test(rows: list[dict], method: str, reference: str = "mace", metric: str = "time_to_pass_s") -> dict:
    """*method* against *reference*, task by task, on *metric*.

    Uses the tasks where both have a value (for time to pass, where both
    passed at least once). Differences are *method* minus *reference*, so a
    positive W means *method* took more.
    """
    by_task: dict[str, dict[str, float]] = defaultdict(dict)
    for s in summarize(rows):
        if s[metric] is not None and s["method"] in (method, reference):
            by_task[s["task"]][s["method"]] = s[metric]
    pairs = {t: v for t, v in by_task.items() if method in v and reference in v}
    diffs = [v[method] - v[reference] for v in pairs.values()]
    w_plus, p = wilcoxon_signed_rank(diffs)
    return {
        "method": method,
        "reference": reference,
        "metric": metric,
        "tasks": len(pairs),
        "median_difference": _median(diffs),
        "w_plus": w_plus,
        "p": p,
    }


def markdown_table(rows: list[dict], columns: list[str]) -> str:
    """*rows* as a Markdown table of *columns*; numbers get two decimals."""

    def cell(value):
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(r.get(c)) for c in columns) + " |" for r in rows]
    return "\n".join(lines)


def codesign_rows(db) -> list[dict]:
    """One row per co-design search: its evaluations, in order, and the
    best feasible finish time found."""
    runs = db.query(
        "SELECT run_id, task, method, repeat, seed, status FROM runs "
        "WHERE task IS NOT NULL AND method LIKE 'codesign_%' ORDER BY started_at",
        (),
    )
    replaced = _replaced(runs)
    rows = []
    for run in runs:
        proposals = db.query(
            "SELECT idx, feasible, sim_time, area_um2, simulated FROM evaluations WHERE run_id = ? ORDER BY idx",
            (run["run_id"],),
        )
        evaluations = [e for e in proposals if e["simulated"]]
        best_so_far, curve = None, []
        for e in evaluations:
            if e["feasible"] and e["sim_time"] is not None:
                best_so_far = e["sim_time"] if best_so_far is None else min(best_so_far, e["sim_time"])
            curve.append(best_so_far)
        rows.append(
            {
                "run_id": run["run_id"],
                "task": run["task"],
                "method": run["method"],
                "repeat": run["repeat"],
                "seed": run["seed"],
                "status": run["status"],
                "replaced": run["run_id"] in replaced,
                "evaluations": len(evaluations),
                "rejected": len(proposals) - len(evaluations),
                "feasible": sum(1 for e in evaluations if e["feasible"]),
                "best_sim_time": best_so_far,
                "curve": curve,
            }
        )
    return rows


def codesign_summary(rows: list[dict], within: float = 0.05) -> list[dict]:
    """One row per (task, method) of co-design searches.

    ``best_known`` is the soonest feasible finish any search of the task
    found. ``sims_to_near_best`` is the median number of simulations a
    search needed to come within *within* of it, over the searches that
    got there; ``reached`` counts those searches.
    """
    rows = _current(rows)
    best_known: dict[str, int] = {}
    for r in rows:
        if r["best_sim_time"] is not None:
            best_known[r["task"]] = min(best_known.get(r["task"], r["best_sim_time"]), r["best_sim_time"])
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["task"], r["method"])].append(r)
    out = []
    for (task, method), group in sorted(groups.items()):
        target = best_known.get(task)
        needed = []
        for r in group:
            if target is None:
                continue
            hit = next((i + 1 for i, b in enumerate(r["curve"]) if b is not None and b <= target * (1 + within)), None)
            if hit is not None:
                needed.append(hit)
        evaluations = sum(r["evaluations"] for r in group)
        out.append(
            {
                "task": task,
                "method": method,
                "searches": len(group),
                "best_sim_time": _median(r["best_sim_time"] for r in group),
                "best_known": target,
                "reached": len(needed),
                "sims_to_near_best": _median(needed),
                "feasible_share": (sum(r["feasible"] for r in group) / evaluations) if evaluations else None,
                "rejected": sum(r["rejected"] for r in group),
            }
        )
    return out
