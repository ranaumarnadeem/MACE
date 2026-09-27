"""One-off: does a 1x1 PicoRV32 tile build and pass under Verilator?

OpenPiton's CI builds PicoRV32 under Verilator (.gitlab-ci.yml's vlt-pico
job: sims -vlt_build -x_tiles=1 -y_tiles=1 -pico) but does not run it: the
run job is commented out, names a stage missing from the stages list, and
asks for -sim_type=msm.

There is no riscv32-unknown-elf toolchain here or in OpenPiton's CI, but
riscv64-unknown-elf-gcc builds rv32ima/ilp32 through multilib, which
-print-multi-directory and a trial assemble and link of this diag
confirmed. PitonConfig.sims_flags() therefore passes
-rv32_target_triple=riscv64-unknown-elf for core="pico", so sims' rv32_as
script uses that compiler.

This script covers 1x1 only; the loop covers the 2x2 and 4x4 meshes.

Run with PITON_ROOT set to a patched OpenPiton checkout:
    PITON_ROOT=~/openpiton python scripts/local_pico_1x1_build_test.py

It passes (status.log "Diag: addi.S-... PASS", sim.log "Info: spc(0)
thread(0) Hit Good trap" and "Simulation -> PASS (HIT GOOD TRAP)") with
three changes, each checked in waveforms:
  - Patch fix 6: picorv32.v's resetn gate opened only on an L15 interrupt
    that a bare configuration never sends, so the core never left reset.
  - Patch fix 7: pc_cmp.v's RTL_PICO0 active_thread tracking waited on the
    same interrupt, so the monitor never saw the core run.
  - The CONFIG_DISABLE_BIST_CLEAR define, which this script sets in
    config_rtl: the generic SRAM model (bram_1rw_wrapper.v) discards writes
    until its power-on BIST self-clear sweep finishes, and PicoRV32 boots
    fast enough to write inside that window. OpenPiton's FPGA flows set the
    same define. The loop's planner requests it through a CONFIG_RTL: line.
"""
from __future__ import annotations

import os
import sys
import time

import ray

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

ROOT = os.environ.get("PITON_ROOT") or sys.exit("Set PITON_ROOT to a patched OpenPiton checkout.")
os.environ["MAKEFLAGS"] = "-j1"  # tonight's OOM precedent; pico should need far less, stay conservative

# address="local" forces a brand-new local instance regardless of any stale
# /tmp/ray/ray_current_cluster marker left by an earlier torn-down cluster.
ray.init(address="local", resources={"openpiton": 1}, log_to_driver=False)

node = OpenPitonWorkspaceNode(ROOT, pg_ready_timeout_s=120)
try:
    print("configuring 1x1 pico...", flush=True)
    cfg = get(node.configure.chia_remote(
        x_tiles=1, y_tiles=1, core="pico",
        config_rtl=("MINIMAL_MONITORING", "CONFIG_DISABLE_BIST_CLEAR"),
    ))
    print(f"build_id={cfg.build_id} key={cfg.key}", flush=True)
    print("sims_flags:", cfg.sims_flags(), flush=True)

    print("building (mirrors upstream CI's own vlt-pico job)...", flush=True)
    started = time.monotonic()
    art = get(node.build.chia_remote(cfg, timeout_seconds=3600))
    wall = time.monotonic() - started
    print(f"build wall_time={wall:.0f}s success={art.success}", flush=True)
    if not art.success:
        print(f"FAILURE_REASON: {art.failure_reason}", flush=True)
        print(f"STDERR_TAIL:\n{art.stderr[-3000:]}", flush=True)
        raise SystemExit(1)
    print(f"binary_path={art.binary_path}", flush=True)
    assert os.path.exists(art.binary_path), "build reported success but binary is missing"
    print("1x1 PICO BUILD: PASS", flush=True)

    print("running addi.S...", flush=True)
    started = time.monotonic()
    res = get(node.run.chia_remote(cfg, "addi.S", timeout_seconds=600))
    wall = time.monotonic() - started
    print(f"run wall_time={wall:.0f}s verdict={res.verdict} success={res.success}", flush=True)
    if res.verdict != "pass":
        print(f"SIM_LOG_TAIL:\n{res.sim_log_tail[-3000:]}", flush=True)
        print(f"STDOUT_TAIL:\n{res.stdout[-1500:]}", flush=True)
        raise SystemExit(1)

    print("1x1 PICO RUN: PASS", flush=True)
finally:
    node.close()
    ray.shutdown()
