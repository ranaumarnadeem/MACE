"""End-to-end MACE loop: Planner -> parallel task execution -> metrics.

Composes every piece built in mace/ (spec, llm, planner, integrator,
metrics) into one real run: a real LLM plans a task DAG for the given
objective, integrate_parallel executes it against real OpenPiton
checkouts, and mace.metrics records the result.

Deliberately depends on BOTH chia_openpiton and mace -- this script (not
either package) is where the two compose, matching the project's layout
rule: chia_openpiton never imports mace, and mace holds no driver script
for a specific run. examples/ is the one place that mixes them.

No tools are given to the LLM this run: the per-task prompt calls (both
the Planner's and each task's, inside integrate_parallel) happen for real,
but with nothing able to edit the checkout -- this proves the real
plan -> execute -> record wiring without also handing a small, free model
unsupervised write access to a working checkout. See mace.loop.
run_mace_step's docstring for the same deliberate split (prove the wiring
first, prove an LLM can write RTL separately).

Run:
    conda activate chia_env
    python examples/mace_end_to_end.py \\
        --piton-root /home/you/openpiton --piton-root-2 /home/you/openpiton-b
"""

from __future__ import annotations

import argparse
import os
import time

import ray

from mace.integrator import integrate_parallel
from mace.llm import make_llm
from mace.metrics import finish_run, open_db, record_iteration, start_run, summary
from mace.planner import PlanningError, plan
from mace.spec import MaceSpec, Task


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--piton-root", required=True, help="First OpenPiton checkout")
    ap.add_argument("--piton-root-2", default=None, help="Second checkout, for fan-out")
    ap.add_argument("--core", default="ariane", choices=("ariane", "sparc"))
    ap.add_argument("--model", default="opencode/big-pickle")
    ap.add_argument(
        "--objective",
        default="Verify the barrier_atomic gate workload passes on a 1x1 mesh.",
    )
    ap.add_argument("--db-path", default=os.path.abspath("runs/mace_end_to_end.db"))
    args = ap.parse_args()

    piton_roots = tuple(
        os.path.abspath(p) for p in (args.piton_root, args.piton_root_2) if p
    )
    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    ray.init(resources={"openpiton": len(piton_roots), "opencode_creds": len(piton_roots)})

    spec = MaceSpec(
        workloads=("barrier_atomic.c",),
        objective=args.objective,
        core=args.core,
        target_mesh=(1, 1),
    )
    llm = make_llm("opencode", model=args.model)

    db = open_db(args.db_path, ray_placement=False)
    run_id = start_run(db, spec)
    print(f"run_id={run_id}")

    print("\n--- planning (real LLM call) ---")
    try:
        tasks = plan(spec, llm)
        for t in tasks:
            print(f"  TASK {t.id}: deps={t.deps} kind={t.kind} spec={t.spec!r}")
    except PlanningError as e:
        print(f"planning failed, falling back to a single hand-written task: {e}")
        tasks = (
            Task(id="verify", deps=(), kind="workload", spec=spec.objective),
        )

    print("\n--- executing ---")
    started = time.time()
    results = integrate_parallel(piton_roots, spec, tasks, llm)
    wall_s = time.time() - started

    for r in results:
        verdict = r.run.verdict if r.run else None
        print(
            f"  {r.task.id}: passed={r.passed} "
            f"build.success={r.build.success} verdict={verdict}"
        )

    record_iteration(db, run_id, 0, results, wall_s)
    all_passed = bool(results) and all(r.passed for r in results)
    finish_run(db, run_id, "passed" if all_passed else "failed")

    print(f"\n--- summary (db={args.db_path}) ---")
    for key, value in summary(db, run_id).items():
        print(f"  {key}: {value}")

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
