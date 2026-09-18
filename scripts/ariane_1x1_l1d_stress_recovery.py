#!/usr/bin/env python3
"""Real-hardware replacement for runs/mace_end_to_end.db's f8f6c1aa1a8f and
4cf5f6027d78: scatter_gather.c on a 1x1 Ariane mesh, first against a
deliberately undersized/direct-mapped L1D (expected to genuinely fail),
then diagnosed for real and re-verified against the corrected geometry.

Those two historical runs recorded "passed" for a task whose spec text
asked for l1d=(128,1), but mace.integrator had no way to thread a per-task
cache override into the build at the time -- every task in a run shared
one PitonConfig built from MaceSpec alone, so both runs actually built and
ran the *default* L1D the whole time regardless of what their task text
claimed (t2's own spec said "expect the verdict to FAIL", and it recorded
passed anyway -- the tell). See mace.spec.Task.caches, mace.agents.
parse_cache_overrides, mace.loop._config_for_task, and mace.integrator.
_run_batch's per-task PitonConfig construction for the fix this verifies.

Uses mace.integrator.integrate (the real, serial single-checkout path) and
mace.triage.triage (one real LLM call against the real failure logs) rather
than mace.orchestrator's full plan/replan loop: the point here is to prove
the infrastructure now honors a task's cache override end to end on real
hardware, not to re-prove an LLM can independently rediscover this exact
plan from prose. Because integrate() correctly stops at the first real
failure (mirroring mace.orchestrator's own iteration boundary), the stress
probe and the corrected retry are two separate integrate() calls, recorded
as iterations 0 and 1 of one run -- exactly how run_mace_loop would have
recorded it had this fired through the full orchestrator.

Run from WSL, real checkout:
    python scripts/ariane_1x1_l1d_stress_recovery.py

Update, first real run (run_id 3d9ae95eecaf): the fix itself is confirmed --
both t1 and t2 recorded caches={"l1d": [128, 1]} in runs/mace_end_to_end.db,
proving the override actually reached the build this time (the historical
runs above recorded caches=None). The stress hypothesis did not pan out,
though: scatter_gather.c passed anyway at this geometry, so a 128-byte
direct-mapped L1D isn't undersized enough to break this workload's
coherence on real hardware -- a genuine negative result, not a bug in the
fix. main() prints "UNEXPECTED" and records status="failed" for exactly
this outcome (both tasks passing the stress probe) rather than treating it
as a crash; re-running as-is will hit the same branch again unless
TINY_L1D is shrunk further (e.g. fewer sets, not just associativity 1) or
a workload with more shared-line traffic is substituted for
scatter_gather.c. Neither change is made here -- picking the next size/
workload to try is a real follow-up, not something to guess at blind.
"""
from __future__ import annotations

import argparse
import os

import ray

from mace.integrator import integrate
from mace.llm import make_llm
from mace.metrics import (
    finish_run,
    mark_all_recovered,
    new_run_id,
    open_db,
    record_failure,
    record_iteration,
    start_run,
    summary,
)
from mace.spec import MaceSpec, StepResult, Task
from mace.triage import triage
from mace.workloads import verify_checksums

TINY_L1D = (("l1d", (128, 1)),)
CORRECTED_L1D = (("l1d", (8192, 4)),)

OBJECTIVE = (
    "Verify the scatter_gather gate workload passes on a 1x1 Ariane mesh, "
    "using a reduced L1 data cache configuration (smaller size and/or lower "
    "associativity than the usual default) to stress-test coherence under "
    "tighter cache capacity. If the first configuration fails, diagnose why, "
    "then propose and verify a corrected configuration that still passes."
)


def _wall_s(results: tuple[StepResult, ...]) -> float:
    return sum(r.build.wall_time_s + (r.run.wall_time_s if r.run else 0.0) for r in results)


def _print_result(r: StepResult) -> None:
    verdict = r.run.verdict if r.run else None
    print(
        f"  {r.task.id}: passed={r.passed} build.success={r.build.success} "
        f"verdict={verdict} caches={r.build.config.caches}"
    )
    if not r.build.success:
        print(f"    build failure_reason: {r.build.failure_reason}")
        print(f"    build stderr (tail): {r.build.stderr[-1000:]}")
    elif r.run is not None and not r.passed and r.run.sim_log_tail:
        print(f"    sim log (tail): {r.run.sim_log_tail[-1000:]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--piton-root",
        default="/mnt/c/Users/Potato/Desktop/openpiton",
        help="OpenPiton checkout on native Linux storage",
    )
    ap.add_argument("--db-path", default=os.path.abspath("runs/mace_end_to_end.db"))
    args = ap.parse_args()

    piton_root = os.path.abspath(args.piton_root)
    if not os.path.isdir(piton_root):
        print(f"ERROR: {piton_root} is not a directory")
        return 2

    verify_checksums()

    spec = MaceSpec(
        workloads=("scatter_gather.c",), objective=OBJECTIVE, core="ariane", target_mesh=(1, 1)
    )
    # No RTL edit is needed for a pure cache-geometry change -- configure()
    # is flags-only here -- so a scripted response is fine for the build/run
    # steps (see mace.loop.run_mace_step's own docstring for why). The one
    # LLM call that actually has to reason for real is the triage() call
    # below, against the real failure this run produces.
    llm = make_llm()

    print(f"Using OpenPiton checkout: {piton_root}")
    print(f"Tiny L1D:      {dict(TINY_L1D)}")
    print(f"Corrected L1D: {dict(CORRECTED_L1D)}")

    ray.init(address="local", resources={"openpiton": 1})
    db = open_db(args.db_path, ray_placement=False)
    run_id = new_run_id()
    start_run(db, spec, run_id=run_id)
    print(f"run_id={run_id}")

    try:
        print("\n=== iteration 0: tiny L1D (128,1), expect scatter_gather to FAIL ===")
        t1 = Task(
            id="t1", deps=(), kind="config",
            spec="Build 1x1 Ariane with l1d=(128,1) (undersized, direct-mapped).",
            caches=TINY_L1D,
        )
        t2 = Task(
            id="t2", deps=("t1",), kind="workload",
            spec="Run scatter_gather.c on the l1d=(128,1) model; expect FAIL.",
            caches=TINY_L1D,
        )
        results_0 = integrate(piton_root, spec, (t1, t2), llm)
        record_iteration(db, run_id, 0, results_0, wall_s=_wall_s(results_0))
        for r in results_0:
            _print_result(r)

        failed = next((r for r in results_0 if not r.passed), None)
        if failed is None:
            print("\n*** UNEXPECTED: both tiny-L1D tasks passed -- the stress probe did not fail. ***")
            finish_run(db, run_id, "failed")
            return 1

        print(f"\n=== real triage on {failed.task.id}'s genuine failure ===")
        diagnosis = triage(failed, llm)
        print(f"  DIAGNOSIS: {diagnosis.diagnosis}")
        print(f"  FIX: {diagnosis.fix}")
        record_failure(db, run_id, 0, failed.task.id, diagnosis.diagnosis, fix=diagnosis.fix)

        print("\n=== iteration 1: corrected L1D (8192,4), expect scatter_gather to PASS ===")
        t4 = Task(
            id="t4", deps=(), kind="config",
            spec="Build 1x1 Ariane with the corrected l1d=(8192,4).",
            caches=CORRECTED_L1D,
        )
        t5 = Task(
            id="t5", deps=("t4",), kind="workload",
            spec="Re-run scatter_gather.c on the corrected model; expect PASS.",
            caches=CORRECTED_L1D,
        )
        results_1 = integrate(piton_root, spec, (t4, t5), llm)
        record_iteration(db, run_id, 1, results_1, wall_s=_wall_s(results_1))
        for r in results_1:
            _print_result(r)

        all_passed = len(results_1) == 2 and all(r.passed for r in results_1)
        status = "passed" if all_passed else "failed"
        if all_passed:
            mark_all_recovered(db, run_id)
        finish_run(db, run_id, status)

        print(f"\n--- summary (db={args.db_path}) ---")
        for key, value in summary(db, run_id).items():
            print(f"  {key}: {value}")
        print(f"\nFinal status: {status}")
        return 0 if all_passed else 1
    finally:
        ray.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
