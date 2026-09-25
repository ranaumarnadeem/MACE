"""Seeded-failure runs: break the loop's first plan and see whether it recovers.

Each run is the loop of examples/mace_end_to_end.py, except that the first
plan's config and workload tasks are changed before any of them execute:

- ``--fault build`` adds ``PITON_FPGA_SYNTH``, a define for FPGA synthesis.
  The Verilator build then fails with ``%Error-PINNOTFOUND``.
- ``--fault sim`` removes ``CONFIG_DISABLE_BIST_CLEAR``. A PicoRV32 mesh then
  builds, and its simulation fails (see docs/05_mace_cores/pico.md).

Later plans are left alone. A replan starts from the objective and the
triage feedback, and never sees the changed plan. The runs are recorded in
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
import dataclasses
import os
import time

import ray

import mace.orchestrator
from mace.llm import default_model_for_backend, make_llm
from mace.metrics import open_db, summary
from mace.planner import plan
from mace.spec import Budget, MaceSpec, Task

FPGA_DEFINE = "PITON_FPGA_SYNTH"
BIST_DEFINE = "CONFIG_DISABLE_BIST_CLEAR"


def break_task(task: Task, fault: str) -> Task:
    """*task* with the seeded fault applied; unit_test tasks are left alone."""
    if task.kind not in ("config", "workload"):
        return task
    flags = set(task.config_rtl or ())
    if fault == "build":
        flags.add(FPGA_DEFINE)
    else:
        flags.discard(BIST_DEFINE)
    return dataclasses.replace(task, config_rtl=tuple(sorted(flags)) or None)


def seeded_plan(fault: str):
    """A stand-in for mace.planner.plan that breaks only its first plan."""
    calls = []

    def first_plan_broken(spec, llm, tools=(), feedback=""):
        tasks = plan(spec, llm, tools=tools, feedback=feedback)
        calls.append(tasks)
        if len(calls) > 1:
            return tasks
        broken = tuple(break_task(t, fault) for t in tasks)
        for before, after in zip(tasks, broken):
            print(f"  seeded {after.id} ({after.kind}): config_rtl {before.config_rtl} -> {after.config_rtl}")
        return broken

    return first_plan_broken


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
    ap.add_argument("--fault", required=True, choices=("build", "sim"))
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
    if args.fault == "sim" and args.core != "pico":
        ap.error("--fault sim removes a define only PicoRV32 needs; use --core pico")
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
        mace.orchestrator.plan = seeded_plan(args.fault)
        started = time.monotonic()
        result = mace.orchestrator.run_mace_loop(piton_roots, spec, llm, db)
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
