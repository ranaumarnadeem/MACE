#!/usr/bin/env python3
"""Run the barrier_atomic.c gate workload on a 1x1 mesh.

Usage:
    conda activate chia_env
    python examples/run_barrier_atomic.py --piton-root /path/to/openpiton
"""

from __future__ import annotations

import argparse
import os
import time

import ray
from chia.base.ChiaFunction import get

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import PitonConfig

from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--piton-root", required=True, help="OpenPiton checkout on native Linux storage")
    ap.add_argument("--core", default="ariane", choices=("ariane", "sparc"))
    ap.add_argument("--x-tiles", type=int, default=1)
    ap.add_argument("--y-tiles", type=int, default=1)
    args = ap.parse_args()

    piton_root = os.path.abspath(args.piton_root)
    if not os.path.isdir(piton_root):
        print(f"ERROR: {piton_root} is not a directory")
        return 1

    # The workload directory contains barrier_atomic.c
    asm_diag_root = str(WORKLOADS_DIR)
    workload = "barrier_atomic.c"

    print(f"Using OpenPiton checkout: {piton_root}")
    print(f"Workload: {workload}")
    print(f"Mesh: {args.x_tiles}x{args.y_tiles}")
    print(f"Core: {args.core}")
    print(f"asm_diag_root: {asm_diag_root}")
    print(f"RTL timeout: {RECOMMENDED_RTL_TIMEOUT} cycles")

    ray.init(resources={"openpiton": 1})

    # Create a node bound to this checkout
    node = OpenPitonWorkspaceNode(piton_root)

    try:
        # Step 1: Configure the 1x1 mesh
        print("\n=== Configuring 1x1 mesh ===")
        config_ref = node.configure.chia_remote(
            x_tiles=args.x_tiles,
            y_tiles=args.y_tiles,
            core=args.core,
        )
        config: PitonConfig = get(config_ref)
        print(f"Config build_id: {config.build_id}")
        print(f"Config key: {config.key}")

        # Step 2: Build the model
        print("\n=== Building Verilator model ===")
        build_ref = node.build.chia_remote(config, clean=True)
        artifact = get(build_ref)
        print(f"Build success: {artifact.success} (rc={artifact.returncode}, {artifact.wall_time_s:.0f}s)")
        if not artifact.success:
            print(f"Failure reason: {artifact.failure_reason}")
            print(artifact.stderr[-3000:])
            return 1
        print(f"Model binary: {artifact.binary_path}")

        # Step 3: Run the barrier_atomic.c workload
        print(f"\n=== Running {workload} ===")
        run_ref = node.run.chia_remote(
            config,
            workload,
            asm_diag_root=asm_diag_root,
            rtl_timeout=RECOMMENDED_RTL_TIMEOUT,
        )
        run_result = get(run_ref)

        print(f"Run success: {run_result.success}")
        print(f"Return code: {run_result.returncode}")
        print(f"Verdict: {run_result.verdict}")
        print(f"Sim time: {run_result.sim_time}")
        print(f"Cycles: {run_result.cycles}")
        print(f"Wall time: {run_result.wall_time_s:.1f}s")
        print(f"Run dir: {run_result.run_dir}")

        if run_result.fake_uart:
            print(f"\n--- UART output ---")
            print(run_result.fake_uart)

        if run_result.sim_log_tail:
            print(f"\n--- Sim log (tail) ---")
            print(run_result.sim_log_tail[-2000:])

        # The workload prints "counter=X expected=Y" to fake_uart
        # and returns 0 on success (counter == num_harts), non-zero on failure
        if run_result.success and run_result.verdict == "pass":
            print("\n*** PASS: barrier_atomic workload passed ***")
            return 0
        else:
            print("\n*** FAIL: barrier_atomic workload failed ***")
            return 1

    finally:
        node.close()


if __name__ == "__main__":
    raise SystemExit(main())