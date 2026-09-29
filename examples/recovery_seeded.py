"""Seeded-failure runs: break the loop's first plan and see whether it recovers.

Each run is the loop of examples/mace_end_to_end.py, except that the first
plan's config and workload tasks are changed before any of them execute, by
one of the faults in mace/eval/faults.py:

- ``--fault build`` (``fpga_synth``) adds ``PITON_FPGA_SYNTH``, a define for
  FPGA synthesis. The Verilator build then fails with ``%Error-PINNOTFOUND``.
- ``--fault sim`` (``drop_bist``) removes ``CONFIG_DISABLE_BIST_CLEAR``. A
  PicoRV32 mesh then builds, and its simulation fails (see
  docs/05_mace_cores/pico.md).

``--fault`` also takes any other fault name there. Later plans are left
alone; the re-plan's feedback names what the failed task changed from the
defaults, which includes the seeded change. The runs are recorded in
their own database, runs/recovery_seeded.db by default. Pass the objective
of the run being compared with --objective; the PicoRV32 runs in the
paper's Table 1 named CONFIG_DISABLE_BIST_CLEAR in theirs.

Run:
    export GOOGLE_CLOUD_PROJECT=<your-gcp-project> MAKEFLAGS=-j1
    python examples/recovery_seeded.py --piton-root ~/openpiton --core ariane \\
        --mesh 2x2 --workload barrier_atomic.c --fault build --runs 3
"""

from __future__ import annotations

import argparse
import os
import time

import ray

from mace.eval.faults import FAULTS, first_plan_breaker
from mace.llm import default_model_for_backend, make_llm
from mace.metrics import open_db, summary
from mace.orchestrator import run_mace_loop
from mace.spec import Budget, MaceSpec

FAULT_ALIASES = {"build": "fpga_synth", "sim": "drop_bist"}


def printing_breaker(fault):
    """first_plan_breaker(fault), printing each change it makes."""
    hook = first_plan_breaker(fault)

    def breaker(iteration, tasks):
        broken = hook(iteration, tasks)
        if iteration == 0:
            for before, after in zip(tasks, broken):
                print(f"  seeded {after.id} ({after.kind}): config_rtl {before.config_rtl} -> {after.config_rtl}, "
                      f"caches {before.caches} -> {after.caches}, network {before.network} -> {after.network}")
        return broken

    return breaker


def _parse_mesh(text: str) -> tuple[int, int]:
    x, sep, y = text.lower().partition("x")
    if not (sep and x.isdigit() and y.isdigit()):
        raise argparse.ArgumentTypeError(f"--mesh must look like 2x2, got {text!r}")
    return int(x), int(y)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--piton-root", required=True)
    ap.add_argument("--core", default="pico", choices=("ariane", "sparc", "pico"))
    ap.add_argument("--mesh", type=_parse_mesh, default=(2, 2))
    ap.add_argument("--workload", default="addi.S")
    ap.add_argument("--fault", required=True, choices=sorted(FAULT_ALIASES) + sorted(FAULTS))
    ap.add_argument(
        "--objective", default=None,
        help="Defaults to the objective examples/mace_end_to_end.py writes from the workload and mesh",
    )
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-iterations", type=int, default=3)
    ap.add_argument("--backend", default="vertex")
    ap.add_argument("--model", default=None)
    ap.add_argument("--project", default=None, help="GCP project; defaults to GOOGLE_CLOUD_PROJECT")
    ap.add_argument("--db-path", default=os.path.abspath("runs/recovery_seeded.db"))
    args = ap.parse_args()
    fault = FAULTS[FAULT_ALIASES.get(args.fault, args.fault)]
    if not fault.applies_to(args.core, args.mesh):
        ap.error(
            f"--fault {args.fault} breaks {sorted(fault.cores)} meshes with at least {fault.min_rows} rows, "
            f"not {args.core} on {args.mesh[0]}x{args.mesh[1]}"
        )
    if args.project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    if args.backend == "vertex" and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        ap.error("--backend vertex needs --project or GOOGLE_CLOUD_PROJECT")
    model = default_model_for_backend(args.model, args.backend)

    piton_roots = (os.path.abspath(os.path.expanduser(args.piton_root)),)
    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    ray.init(
        address="local",
        resources={"openpiton": 1, f"{args.backend}_creds": 1},
        include_dashboard=False,
    )
    spec = MaceSpec(
        workloads=(args.workload,),
        objective=args.objective
        or f"Verify the {args.workload} gate workload passes on a {args.mesh[0]}x{args.mesh[1]} mesh.",
        core=args.core,
        target_mesh=args.mesh,
        budget=Budget(max_iterations=args.max_iterations),
    )
    llm = make_llm(args.backend, **({"model": model} if model else {}))
    db = open_db(args.db_path, ray_placement=False)
    print(f"objective: {spec.objective}", flush=True)

    rows = []
    for n in range(1, args.runs + 1):
        print(f"\n=== run {n}/{args.runs}: fault={args.fault} core={args.core} mesh={args.mesh} ===", flush=True)
        started = time.monotonic()
        result = run_mace_loop(piton_roots, spec, llm, db, plan_hook=printing_breaker(fault))
        wall = time.monotonic() - started
        for i, iteration in enumerate(result.iterations):
            for r in iteration:
                verdict = r.run.verdict if r.run else None
                print(f"  iteration {i}: {r.task.id} ({r.task.kind}) passed={r.passed} "
                      f"build={r.build.success} verdict={verdict}", flush=True)
            for row in db.query(
                "SELECT task_id, diagnosis, fix FROM failures WHERE run_id = ? AND iteration = ?",
                (result.run_id, i),
            ):
                print(f"  triage[{row['task_id']}]: {row['diagnosis']} -- {row['fix']}", flush=True)
        stats = summary(db, result.run_id)
        rows.append((result.run_id, result.status, len(result.iterations), wall))
        print(f"  run_id={result.run_id} status={result.status} iterations={len(result.iterations)} "
              f"wall={wall:.1f}s failures_recovered={stats.get('failures_recovered')}", flush=True)

    print(f"\n--- fault={args.fault}: {sum(s == 'passed' for _, s, _, _ in rows)}/{len(rows)} recovered ---")
    for run_id, status, iterations, wall in rows:
        print(f"  {run_id}  {status:<16} iterations={iterations}  wall={wall:.1f}s")
    ray.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
