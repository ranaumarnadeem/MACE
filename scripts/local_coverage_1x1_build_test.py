"""One-off: does Verilator code coverage work end to end against real Ariane
RTL -- build, run to a real pass, and a real coverage report?

Nothing in chia_openpiton needs to change for this: `-vlt_build_args` already
passes any flag straight through to the real `verilator` invocation unmangled
(confirmed by reading sims,2.0 directly -- no whitelist, no comma-splitting).
The one real gap was scripts/patch_openpiton.sh fix 5: the hand-written
testbench Verilator links against (piton/tools/verilator/my_top.cpp) never
called Verilator's own coverage-write API, so even a run that completed
cleanly produced no coverage.dat. Run patch_openpiton.sh against this
checkout before running this script, or it will build fine and then find no
coverage.dat to report on.

A first, uncontrolled attempt at this (interactively, not from this script)
used no rtl_timeout override and got verdict=timeout -- turned out to be
nothing to do with coverage: that attempt also omitted rtl_timeout, which
defaults to a much smaller value (+TIMEOUT's own 50000-cycle default,
manycore.config) than mace/workloads.py's own RECOMMENDED_RTL_TIMEOUT
(1_000_000) already proven necessary for this exact workload -- the same gap
mace/loop.py itself shipped with once (see the plan doc's own §15). With that
controlled for, coverage adds no meaningful wall-clock overhead worth noting
(62s run, same order of magnitude as a non-coverage run).

Real, load-bearing gotcha this script works around: this machine has *two*
Verilator installs. The one `conda activate chia_env` puts first on PATH
(/usr/local/bin, "5.049 devel") is what real builds actually use -- but its
verilator_coverage binary is broken outright (crashes with "internal fault,
sorry" even on --version, not something to do with coverage.dat's content).
The system apt install (/usr/bin, 5.020) has a working verilator_coverage,
confirmed against this exact script's own coverage.dat -- but it predates
--report entirely (only --annotate/--write/--write-info exist in that
version's CLI). This script deliberately invokes /usr/bin/verilator_coverage
by absolute path for exactly that reason: coverage.dat's own format is a
Verilator-internal detail, not tied to which binary happens to be first on
PATH, and the stable tool is what actually works here. --annotate gives a
real top-level "Total coverage (N/M) XX.XX%" line plus a full per-file
annotated-source dump (uncovered lines marked %00) -- genuinely per-module,
just not as a single pre-computed percentage table the way --report hier
would have given if it existed on this Verilator version.

Run from WSL, real checkout, after scripts/patch_openpiton.sh:
    python scripts/local_coverage_1x1_build_test.py
"""
from __future__ import annotations

import os
import subprocess
import time

import ray

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from mace.workloads import RECOMMENDED_RTL_TIMEOUT

ROOT = "/mnt/c/Users/Potato/Desktop/openpiton"
os.environ["MAKEFLAGS"] = "-j1"  # this project's own standing OOM precedent

# address="local" forces a brand-new local instance regardless of any stale
# /tmp/ray/ray_current_cluster marker left by an earlier torn-down cluster.
ray.init(address="local", resources={"openpiton": 1}, log_to_driver=False)

node = OpenPitonWorkspaceNode(ROOT, pg_ready_timeout_s=120)
try:
    print("configuring 1x1 ariane with --coverage-line...", flush=True)
    cfg = get(node.configure.chia_remote(
        x_tiles=1, y_tiles=1, core="ariane",
        extra_flags=("-vlt_build_args=--coverage-line",),
    ))
    print(f"build_id={cfg.build_id}", flush=True)

    print("building...", flush=True)
    started = time.monotonic()
    art = get(node.build.chia_remote(cfg, timeout_seconds=1800))
    wall = time.monotonic() - started
    print(f"build wall_time={wall:.0f}s success={art.success}", flush=True)
    if not art.success:
        print(f"FAILURE_REASON: {art.failure_reason}", flush=True)
        print(f"STDERR_TAIL:\n{art.stderr[-4000:]}", flush=True)
        raise SystemExit(1)
    print(f"binary_path={art.binary_path}", flush=True)
    assert os.path.exists(art.binary_path), "build reported success but binary is missing"
    print("COVERAGE BUILD: PASS", flush=True)

    print(f"running hello_world.c with rtl_timeout={RECOMMENDED_RTL_TIMEOUT}...", flush=True)
    started = time.monotonic()
    res = get(node.run.chia_remote(
        cfg, "hello_world.c", rtl_timeout=RECOMMENDED_RTL_TIMEOUT, timeout_seconds=1800,
    ))
    wall = time.monotonic() - started
    print(f"run wall_time={wall:.0f}s verdict={res.verdict} success={res.success}", flush=True)
    print(f"run_dir={res.run_dir}", flush=True)
    if res.verdict != "pass":
        print(f"SIM_LOG_TAIL:\n{res.sim_log_tail[-3000:]}", flush=True)
        raise SystemExit(1)
    print("COVERAGE RUN: PASS", flush=True)

    dat_path = os.path.join(res.run_dir, "coverage.dat")
    assert os.path.exists(dat_path), f"run passed but no coverage.dat at {dat_path}"
    print(f"coverage.dat found: {dat_path} ({os.path.getsize(dat_path)} bytes)", flush=True)

    annotate_dir = os.path.join(res.run_dir, "coverage_annotated")
    print(f"running /usr/bin/verilator_coverage --annotate {annotate_dir} ...", flush=True)
    report = subprocess.run(
        ["/usr/bin/verilator_coverage", "--annotate", annotate_dir, dat_path],
        capture_output=True, text=True, timeout=120,
    )
    print(f"verilator_coverage exit={report.returncode}", flush=True)
    print(report.stdout, flush=True)
    if report.stderr:
        print(f"STDERR:\n{report.stderr}", flush=True)
    if report.returncode != 0:
        raise SystemExit(1)
    annotated_files = os.listdir(annotate_dir) if os.path.isdir(annotate_dir) else []
    print(f"{len(annotated_files)} annotated source file(s) written to {annotate_dir}", flush=True)
    assert annotated_files, "verilator_coverage exited 0 but wrote no annotated files"
    print("COVERAGE REPORT: PASS -- real total + real per-file annotated coverage produced end to end", flush=True)
finally:
    node.close()
    ray.shutdown()
