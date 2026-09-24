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


def _parse_mesh(text: str) -> tuple[int, int]:
    x, sep, y = text.lower().partition("x")
    if not (sep and x.isdigit() and y.isdigit()):
        raise argparse.ArgumentTypeError(f"--mesh must look like 2x2, got {text!r}")
    return int(x), int(y)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--piton-root", required=True, help="First OpenPiton checkout")
    ap.add_argument("--piton-root-2", default=None, help="Second checkout, for fan-out")
    ap.add_argument("--core", default="ariane", choices=("ariane", "sparc", "pico"))
    ap.add_argument("--backend", default="vertex", help="LLM backend (default: vertex)")
    ap.add_argument(
        "--model", default=None,
        help="Defaults to gemini-2.5-flash for --backend vertex; other backends use "
        "their own default model unless one is given explicitly here.",
    )
    ap.add_argument(
        "--project", default=None,
        help="GCP project for --backend vertex; defaults to GOOGLE_CLOUD_PROJECT",
    )
    ap.add_argument("--workload", default="barrier_atomic.c")
    ap.add_argument(
        "--mesh", type=_parse_mesh, default=(1, 1),
        help="Target mesh as XxY. This, not the objective text, sizes every task's build.",
    )
    ap.add_argument("--objective", default=None)
    ap.add_argument("--max-iterations", type=int, default=3)
    ap.add_argument("--db-path", default=os.path.abspath("runs/mace_end_to_end.db"))
    args = ap.parse_args()
    args.model = default_model_for_backend(args.model, args.backend)
    if args.project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    if args.backend == "vertex" and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        ap.error("--backend vertex needs --project or GOOGLE_CLOUD_PROJECT")

    piton_roots = tuple(
        os.path.abspath(p) for p in (args.piton_root, args.piton_root_2) if p
    )
    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    # address="local" forces a brand-new local instance regardless of any stale
    # /tmp/ray/ray_current_cluster marker left by an earlier torn-down cluster
    # (e.g. a `chia up`/`chia down` session) -- without it, ray.init() can
    # silently try to attach to that dead address instead of starting fresh.
    # include_dashboard=False: nothing here uses the dashboard UI. Does not
    # fix it, but on a loaded machine ray.init() can still hit a real,
    # separate Ray startup race: the per-node dashboard AGENT (which starts
    # regardless of this flag) sometimes takes longer to load its modules
    # than the raylet's own internal wait for the agent's listen-port file,
    # so the raylet crashes on startup ("Timed out waiting for file
    # .../dashboard_agent_listen_port_..."). No fix found within ray.init()
    # itself -- retrying (deleting /tmp/ray/ray_current_cluster first) is
    # what actually recovers.
    ray.init(
        address="local",
        resources={"openpiton": len(piton_roots), f"{args.backend}_creds": len(piton_roots)},
        include_dashboard=False,
    )

    objective = args.objective or (
        f"Verify the {args.workload} gate workload passes on a {args.mesh[0]}x{args.mesh[1]} mesh."
    )
    spec = MaceSpec(
        workloads=(args.workload,),
        objective=objective,
        core=args.core,
        target_mesh=args.mesh,
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
