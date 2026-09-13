#!/usr/bin/env python3
"""Run the scatter_gather.c gate workload on the reduced-L1D 1x1 Ariane config.

The reduced config (same as scripts/ariane_1x1_small_l1d_build.py), corrected
per t3 diagnosis -- capacity reduced, associativity kept at default:
    l1d = (4096, 4)   # reduced capacity: default is (8192, 4)
    l1i = (16384, 4)  # default
    l15 = (8192, 4)   # default
    l2  = (65536, 4)  # default

(The original probe used l1d=(4096, 2), reducing both capacity and
associativity; the corrected geometry reduces only one dimension.)

`configure(caches=...)` REPLACES the cache dict, it does not merge, so every
cache is spelled out here. The config key hashes the cache geometry, so this
lands in its own model directory (build_id mace_e83f1ec39a11 for this
checkout/revisions).

Pass rule: the run is a pass only when the transcript verdict reads "pass"
(PitonRunResult.decide). On any failure the verdict, the sim-log tail, the
status.log, and the run directory (which keeps sim.log/status.log/mem.image/
fake_uart.log on disk) are all printed/retained for diagnosis, and the script
exits non-zero.

Run from WSL, real checkout:
    python scripts/ariane_1x1_small_l1d_scatter_gather.py
"""
from __future__ import annotations

import argparse
import os

import ray
from chia.base.ChiaFunction import get

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import PitonConfig

from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR, verify_checksums

# Reduced-L1D cache geometry (corrected per t3 diagnosis): capacity halved,
# associativity kept at the default 4.
SMALL_L1D_CACHES = {
    "l1i": (16384, 4),
    "l1d": (4096, 4),
    "l15": (8192, 4),
    "l2": (65536, 4),
}


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
        return 1

    asm_diag_root = str(WORKLOADS_DIR)
    workload = "scatter_gather.c"

    # The gate workload is the pass/fail oracle; make sure it is pristine
    # before trusting any verdict derived from it.
    verify_checksums()

    print(f"Using OpenPiton checkout: {piton_root}")
    print(f"Workload: {workload}")
    print(f"Mesh: 1x1")
    print(f"Core: ariane")
    print(f"Caches: {dict(sorted(SMALL_L1D_CACHES.items()))}")
    print(f"asm_diag_root: {asm_diag_root}")
    print(f"RTL timeout: {RECOMMENDED_RTL_TIMEOUT} cycles")

    # address="local": see examples/mace_end_to_end.py's ray.init() comment --
    # avoids silently attaching to a stale torn-down cluster's marker.
    ray.init(address="local", resources={"openpiton": 1})
    node = OpenPitonWorkspaceNode(piton_root)

    try:
        # Step 1: Configure the reduced-L1D 1x1 Ariane mesh
        print("\n=== Configuring reduced-L1D 1x1 mesh ===")
        config: PitonConfig = get(
            node.configure.chia_remote(
                x_tiles=1,
                y_tiles=1,
                core="ariane",
                network_config="2dmesh_config",
                caches=dict(SMALL_L1D_CACHES),
            )
        )
        print(f"Config build_id: {config.build_id}")
        print(f"Config key: {config.key[:16]}...")
        print(f"Caches: {dict(sorted(config.caches.items()))}")

        # Step 2: Build the model (served from disk if this build_id exists)
        print("\n=== Building Verilator model ===")
        artifact = get(node.build.chia_remote(config))
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

        if run_result.fake_uart:
            print("\n--- UART output ---")
            print(run_result.fake_uart)

        if run_result.verdict == "pass" and run_result.success:
            print("\n*** PASS: scatter_gather passed on the reduced-L1D config ***")
            return 0

        # --- Failure path: keep everything needed to diagnose the failure ---
        print("\n*** FAIL: scatter_gather did NOT pass on the reduced-L1D config ***")
        # Full run artifacts stay on disk in run_result.run_dir (sim.log,
        # status.log, mem.image, fake_uart.log, ...). Re-print the tail and
        # status.log here so the verdict + diagnostics are captured even if
        # the run_dir is later reclaimed.
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