"""End-to-end MACE loop: plan -> execute -> triage -> replan -> metrics.

Composes every piece built in mace/ into one real run via
mace.orchestrator.run_mace_loop: a real LLM plans a task DAG, executes it
against real OpenPiton checkouts, triages and replans on failure, and
mace.metrics records every iteration -- until something passes or the
spec's budget runs out.

Deliberately depends on BOTH chia_openpiton and mace -- this script (not
either package) is where the two compose, matching the project's layout
rule: chia_openpiton never imports mace, and mace holds no driver script
for a specific run. examples/ is the one place that mixes them.

No tools are given to the LLM this run -- see mace.loop.run_mace_step's
docstring for why (prove the wiring first, prove an LLM can write RTL
separately).

Run:
    conda activate chia_env
    python examples/mace_end_to_end.py \\
        --piton-root /home/you/openpiton --piton-root-2 /home/you/openpiton-b
"""

from __future__ import annotations

import argparse
import os

import ray

from mace.llm import default_model_for_backend, make_llm
from mace.metrics import open_db, summary
from mace.orchestrator import run_mace_loop
from mace.spec import Budget, MaceSpec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--piton-root", required=True, help="First OpenPiton checkout")
    ap.add_argument("--piton-root-2", default=None, help="Second checkout, for fan-out")
    ap.add_argument("--core", default="ariane", choices=("ariane", "sparc", "pico"))
    ap.add_argument("--backend", default="vertex", help="LLM backend -- vertex is this project's only funded one")
    ap.add_argument(
        "--model", default=None,
        help="Defaults to gemini-2.5-flash for --backend vertex; other backends use "
        "their own default model unless one is given explicitly here.",
    )
    ap.add_argument("--project", default="mace-508004")
    ap.add_argument("--workload", default="barrier_atomic.c")
    ap.add_argument(
        "--objective",
        default="Verify the barrier_atomic gate workload passes on a 1x1 mesh.",
    )
    ap.add_argument("--max-iterations", type=int, default=3)
    ap.add_argument("--db-path", default=os.path.abspath("runs/mace_end_to_end.db"))
    args = ap.parse_args()
    args.model = default_model_for_backend(args.model, args.backend)

    piton_roots = tuple(
        os.path.abspath(p) for p in (args.piton_root, args.piton_root_2) if p
    )
    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", args.project)
    # address="local" forces a brand-new local instance regardless of any stale
    # /tmp/ray/ray_current_cluster marker left by an earlier torn-down cluster
    # (e.g. a `chia up`/`chia down` session) -- without it, ray.init() can
    # silently try to attach to that dead address instead of starting fresh.
    ray.init(
        address="local",
        resources={"openpiton": len(piton_roots), f"{args.backend}_creds": len(piton_roots)},
    )

    spec = MaceSpec(
        workloads=(args.workload,),
        objective=args.objective,
        core=args.core,
        target_mesh=(1, 1),
        budget=Budget(max_iterations=args.max_iterations),
    )
    llm = make_llm(args.backend, **({"model": args.model} if args.model else {}))
    db = open_db(args.db_path, ray_placement=False)

    result = run_mace_loop(piton_roots, spec, llm, db)

    print(f"run_id={result.run_id} status={result.status}")
    for i, iteration_results in enumerate(result.iterations):
        print(f"\n--- iteration {i} ---")
        for r in iteration_results:
            verdict = r.run.verdict if r.run else None
            print(
                f"  {r.task.id} ({r.task.kind}): passed={r.passed} "
                f"build.success={r.build.success} verdict={verdict}"
            )
        for row in db.query(
            "SELECT task_id, diagnosis, fix FROM failures WHERE run_id = ? AND iteration = ?",
            (result.run_id, i),
        ):
            print(f"  triage[{row['task_id']}]: {row['diagnosis']} -- {row['fix']}")

    print(f"\n--- summary (db={args.db_path}) ---")
    for key, value in summary(db, result.run_id).items():
        print(f"  {key}: {value}")

    if result.post_mortem is not None:
        pm = result.post_mortem
        print(f"\n--- post-mortem: {pm.assessment} ---")
        print(f"  {pm.explanation}")
        if pm.next_steps:
            print(f"  next steps: {pm.next_steps}")

    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
