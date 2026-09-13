#!/usr/bin/env python3
"""Run the producer_consumer.c gate workload on a 1x1 mesh."""

from __future__ import annotations

import argparse
import os

import ray
from chia.base.ChiaFunction import get

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import PitonConfig

from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--piton-root", required=True)
    ap.add_argument("--core", default="ariane", choices=("ariane", "sparc"))
    ap.add_argument("--x-tiles", type=int, default=1)
    ap.add_argument("--y-tiles", type=int, default=1)
    args = ap.parse_args()

    piton_root = os.path.abspath(args.piton_root)
    asm_diag_root = str(WORKLOADS_DIR)
    workload = "producer_consumer.c"

    print(f"Workload: {workload}")
    print(f"Mesh: {args.x_tiles}x{args.y_tiles}")
    print(f"Core: {args.core}")
    print(f"RTL timeout: {RECOMMENDED_RTL_TIMEOUT} cycles")

    # address="local": see examples/mace_end_to_end.py's ray.init() comment --
    # avoids silently attaching to a stale torn-down cluster's marker.
    ray.init(address="local", resources={"openpiton": 1})
    node = OpenPitonWorkspaceNode(piton_root)

    try:
        print("\n=== Configuring 1x1 mesh ===")
        config: PitonConfig = get(
            node.configure.chia_remote(
                x_tiles=args.x_tiles,
                y_tiles=args.y_tiles,
                core=args.core,
            )
        )
        print(f"Config build_id: {config.build_id}")

        print("\n=== Building Verilator model ===")
        artifact = get(node.build.chia_remote(config, clean=True))
        print(f"Build success: {artifact.success} ({artifact.wall_time_s:.0f}s)")
        if not artifact.success:
            print(f"Failure: {artifact.failure_reason}")
            print(artifact.stderr[-3000:])
            return 1

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
        print(f"Sim time: {run_result.sim_time}")
        print(f"Wall time: {run_result.wall_time_s:.1f}s")
        print(f"Run dir: {run_result.run_dir}")

        if run_result.fake_uart:
            print(f"\n--- UART output ---")
            print(run_result.fake_uart)

        if run_result.sim_log_tail:
            print(f"\n--- Sim log (tail) ---")
            print(run_result.sim_log_tail[-2000:])

        if run_result.success and run_result.verdict == "pass":
            print("\n*** Simulation -> PASS ***")
            return 0
        else:
            print("\n*** FAIL ***")
            return 1
    finally:
        node.close()


if __name__ == "__main__":
    raise SystemExit(main())
