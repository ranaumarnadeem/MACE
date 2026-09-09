"""One-off: does a 1x1 PicoRV32 tile build AND pass under Verilator?

OpenPiton's own CI builds PicoRV32 under Verilator (.gitlab-ci.yml's
vlt-pico job: sims -vlt_build -x_tiles=1 -y_tiles=1 -pico) but has never
run it under any simulator, Verilator included -- the run job is commented
out, targets a stage that doesn't even exist in the stages list, and even
when live specified -sim_type=msm, never vlt. A real pass/fail verdict
here would be a first, by anyone, not just this project.

See the plan doc section 19 for the full toolchain investigation: no
riscv32-unknown-elf toolchain exists on this machine or in OpenPiton's own
CI, but the installed riscv64-unknown-elf-gcc supports rv32ima/ilp32 via
multilib, confirmed by both -print-multi-directory and a real trial
assemble+link of this exact diag (Phase 0, already done, passed cleanly).
chia_openpiton.state_def.PitonConfig.sims_flags() now emits
-rv32_target_triple=riscv64-unknown-elf for core="pico" to make sims'
own rv32_as script use that compiler.

Deliberately 1x1 only -- see the plan doc for why multi-tile pico is out
of scope for this pass.

Run from WSL, real checkout:
    python scripts/local_pico_1x1_build_test.py
"""
from __future__ import annotations

import os
import time

import ray

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

ROOT = "/mnt/c/Users/Potato/Desktop/openpiton"
os.environ["MAKEFLAGS"] = "-j1"  # tonight's OOM precedent; pico should need far less, stay conservative

# address="local" forces a brand-new local instance regardless of any stale
# /tmp/ray/ray_current_cluster marker left by an earlier torn-down cluster.
ray.init(address="local", resources={"openpiton": 1}, log_to_driver=False)

node = OpenPitonWorkspaceNode(ROOT, pg_ready_timeout_s=120)
try:
    print("configuring 1x1 pico...", flush=True)
    cfg = get(node.configure.chia_remote(x_tiles=1, y_tiles=1, core="pico"))
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

    print("running addi.S (never run under any simulator before, by anyone)...", flush=True)
    started = time.monotonic()
    res = get(node.run.chia_remote(cfg, "addi.S", timeout_seconds=600))
    wall = time.monotonic() - started
    print(f"run wall_time={wall:.0f}s verdict={res.verdict} success={res.success}", flush=True)
    if res.verdict != "pass":
        print(f"SIM_LOG_TAIL:\n{res.sim_log_tail[-3000:]}", flush=True)
        print(f"STDOUT_TAIL:\n{res.stdout[-1500:]}", flush=True)
        raise SystemExit(1)

    print("1x1 PICO RUN: PASS -- first Verilator pass for PicoRV32, ever", flush=True)
finally:
    node.close()
    ray.shutdown()
