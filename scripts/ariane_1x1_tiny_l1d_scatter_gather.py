#!/usr/bin/env python3
"""Run the scatter_gather.c gate workload on the tiny-L1D 1x1 Ariane config.

Cache geometry under test (the stress-tested extreme):
    l1d = (128, 1)    # minimal capacity AND associativity: a single 128-byte
                      # direct-mapped set; default l1d is (8192, 4)
    l1i = (16384, 4)  # default (unchanged)
    l15 = (8192, 4)   # default (unchanged)
    l2  = (65536, 4)  # default (unchanged)

This is the deliberate probe: with one 128-byte direct-mapped L1D set, every
hart's ATOMIC_OP write and the barrier spin (all through the shared slots[]
/arrived array) alias into the same cache line repeatedly. The workload is
correct (no two harts write the same slot), so any failure verdict here is a
real visibility/coherence consequence of the tiny geometry, not a race in the
program itself.

`configure(caches=...)` REPLACES the cache dict, it does not merge, so every
cache is spelled out here. The config key hashes the cache geometry, so this
lands in its own model directory (distinct build_id) and cannot collide with
the default-, the (4096,2)-, or the (4096,4)-geometry 1x1 Ariane models.

Pass rule: the run is a pass only when the transcript verdict reads "pass"
(PitonRunResult.decide). On any failure the verdict, the run directory (which
keeps sim.log/status.log/mem.image/fake_uart.log on disk -- retained for
diagnosis), the UART output, the sim-log tail, and the status.log are all
printed, and the script exits non-zero.

Run from WSL, real checkout:
    python scripts/ariane_1x1_tiny_l1d_scatter_gather.py
"""
from __future__ import annotations

import argparse
import os

import ray
from chia.base.ChiaFunction import get

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import DEFAULT_CACHES, PitonConfig

from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR, verify_checksums

# Tiny-L1D cache geometry: l1d dropped to a single 128-byte direct-mapped set.
# Everything else stays at OpenPiton's defaults.
TINY_L1D_CACHES = {
    **dict(DEFAULT_CACHES),
    "l1d": (128, 1),
}

# Since the tiny-l1d model has never been built in this checkout, the first
# invocation is a full Verilator RTL compile (15-60 min). 7200 s matches the
# adapter's own build timeout; nothing in this script is allowed to slip past it.
BUILD_TIMEOUT_S = 7200


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--piton-root",
        default="/mnt/c/Users/Potato/Desktop/openpiton",
        help="OpenPiton checkout on native Linux storage",
    )
    args = ap.parse_args()

    piton_root = os.path.abspath(args.piton_root)
    if not os.path.isdir(piton_root):
        print(f"ERROR: {piton_root} is not a directory")
        return 2

    asm_diag_root = str(WORKLOADS_DIR)
    workload = "scatter_gather.c"

    # The gate workload is the pass/fail oracle; make sure it is pristine
    # before trusting any verdict derived from it.
    verify_checksums()

    print(f"Using OpenPiton checkout: {piton_root}")
    print(f"Workload: {workload}")
    print(f"Mesh: 1x1")
    print(f"Core: ariane")
    print(f"Caches: {dict(sorted(TINY_L1D_CACHES.items()))}")
    print(f"asm_diag_root: {asm_diag_root}")
    print(f"RTL timeout: {RECOMMENDED_RTL_TIMEOUT} cycles")

    # address="local": see examples/mace_end_to_end.py's ray.init() comment --
    # avoids silently attaching to a stale torn-down cluster's marker.
    ray.init(address="local", resources={"openpiton": 1})
    node = OpenPitonWorkspaceNode(piton_root)

    try:
        # Step 1: Configure the tiny-L1D 1x1 Ariane mesh
        print("\n=== Configuring tiny-L1D 1x1 mesh (l1d=(128,1)) ===")
        config: PitonConfig = get(
            node.configure.chia_remote(
                x_tiles=1,
                y_tiles=1,
                core="ariane",
                network_config="2dmesh_config",
                caches=dict(TINY_L1D_CACHES),
            )
        )
        print(f"Config build_id: {config.build_id}")
        print(f"Config key: {config.key[:16]}...")
        print(f"Caches: {dict(sorted(config.caches.items()))}")

        # Step 2: Build the Verilator model (served from disk if this build_id
        # already has a confirmed-good marker; fresh compile otherwise).
        print("\n=== Building Verilator model ===")
        artifact = get(
            node.build.chia_remote(config, timeout_seconds=BUILD_TIMEOUT_S)
        )
        print(
            f"Build success: {artifact.success} "
            f"(reused={artifact.reused}, rc={artifact.returncode}, "
            f"{artifact.wall_time_s:.0f}s)"
        )
        if not artifact.success:
            print(f"Failure reason: {artifact.failure_reason}")
            print(artifact.stderr[-3000:])
            return 1
        print(f"Model binary: {artifact.binary_path}")

        # Step 3: Run the scatter_gather.c workload
        print(f"\n=== Running {workload} ===")
        run_result = get(
            node.run.chia_remote(
                config,
                workload,
                asm_diag_root=asm_diag_root,
                rtl_timeout=RECOMMENDED_RTL_TIMEOUT,
            )
        )

        print(f"Verdict: {run_result.verdict}")
        print(f"Return code: {run_result.returncode}")
        print(f"Success: {run_result.success}")
        print(f"Sim time: {run_result.sim_time}")
        print(f"Cycles: {run_result.cycles}")
        print(f"Wall time: {run_result.wall_time_s:.1f}s")
        print(f"Run dir: {run_result.run_dir}")

        if run_result.verdict == "pass" and run_result.success:
            if run_result.fake_uart:
                print("\n--- UART output ---")
                print(run_result.fake_uart)
            print("\n*** PASS: scatter_gather passed on the tiny-L1D config ***")
            return 0

        # --- Failure path (expected for this deliberate probe): keep
        # everything needed to diagnose the failure. The run artifacts stay on
        # disk in run_result.run_dir (sim.log, status.log, mem.image,
        # fake_uart.log, ...); re-print the UART output, sim-log tail and
        # status.log here so the verdict + diagnostics are captured even if
        # the run_dir is later reclaimed.
        print("\n*** FAIL: scatter_gather did NOT pass on the tiny-L1D config ***")
        print(f"Run dir retained for diagnosis: {run_result.run_dir}")
        if run_result.fake_uart:
            print("\n--- UART output ---")
            print(run_result.fake_uart)
        if run_result.sim_log_tail:
            print("\n--- Sim log (tail) ---")
            print(run_result.sim_log_tail)
        if run_result.status_log:
            print("\n--- status.log ---")
            print(run_result.status_log)
        return 1
    finally:
        node.close()
        ray.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())