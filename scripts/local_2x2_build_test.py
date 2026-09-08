"""One-off: does a multi-tile Ariane mesh actually build and pass on real
hardware?

The GCP acceptance test (chia_openpiton/test/cluster/openpiton_e2e_test.py::
TestAcceptance2) is blocked on a chia-side tailnet-relay issue that has
nothing to do with OpenPiton/mesh size -- but the technical claim it exists
to prove (multi-tile Ariane coherence) was always documented with a local
stand-in ("Docker image on WSL" in the original plan). This is that stand-in,
run directly (no Docker -- see the plan's Phase 1 retrospective on why
Docker was dropped entirely).

Every real run so far (this whole project) has only ever been 1x1. First
attempt at anything larger was 2x2 (4 tiles): the build succeeded (after
fixing three real environment bugs -- a broken git-symlink checkout, a
missing toolchain patch, 66 CRLF-broken shebangs), but the RUN hung --
fake_uart.log showed only hart 0 ever said hello ("hart 0 of 4 harts"),
and only trace_hart_00.dasm was ever produced; harts 1-3 never booted.
Raising max_cycle 1.5M -> 10M changed nothing (identical hang point, just
took longer to hit the cap), ruling out "just needs more cycles."

Checked OpenPiton's own .gitlab-ci.yml + master_diaglist_princeton: the
ONLY Verilator-validated multi-tile Ariane config upstream is
ariane_tile16_simple at exactly 4x4 (16 tiles) -- 2x2 has no upstream
precedent at all under any simulator. That's the likely reason: this may
be a genuine, previously-undiscovered boot-distribution bug specific to
non-4x4 multi-tile shapes, not something wrong in this project's own code.
Rather than debug OpenPiton's own untested-shape RTL blind, pivot to the
one multi-tile config actually known to work, using the diaglist's own
exact args (master_diaglist_princeton:429-435): -finish_mask (one '1' per
hart) and -rtl_timeout 10000000, hello_world_many.c.

Run from WSL, real checkout, real toolchain already proven:
    python scripts/local_2x2_build_test.py
"""
from __future__ import annotations

import os
import time

import ray

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

ROOT = "/mnt/c/Users/Potato/Desktop/openpiton"
# binutils 2.38+ split zicsr/zifencei out of base RV64I; OpenPiton's 2019
# diags need it spelled out (see chia_openpiton/test/cluster/openpiton_e2e_test.py).
ZICSR = ("-rv64_march=rv64imafdc_zicsr_zifencei",)
X_TILES, Y_TILES = 4, 4
FINISH_MASK = "1" * (X_TILES * Y_TILES)

# The first 4x4 attempt died to ray.exceptions.OutOfMemoryError: 22.33GB /
# 23.47GB used, killed while many parallel cc1plus processes (compiling
# Verilator's generated C++ for the much bigger 16-tile design) were each
# holding 100MB-850MB. That explains every "WSL just crashed" interruption
# tonight too, not just this one clean Ray-level kill -- WSL2's own VM
# memory cap (.wslconfig: 24GB, already right at this host's practical
# ceiling -- raising it further would starve Windows itself instead) was
# almost certainly what the earlier raw crashes were actually hitting,
# before Ray's own OOM monitor had a chance to intervene cleanly.
# MAKEFLAGS is respected by GNU Make automatically for any `make` call that
# doesn't itself pass -j -- including whatever sims's own Perl invokes to
# build the generated C++ -- so setting it here, before a single subprocess
# spawns, propagates the same way RISCV/PATH already do throughout this
# codebase. -j2 trades build time for headroom; worth it after tonight.
os.environ["MAKEFLAGS"] = "-j1"

# address="local" forces a brand-new local instance regardless of any
# stale /tmp/ray/ray_current_cluster marker left by an earlier torn-down
# cluster (ray.init() with no address falls back to that file and tries
# to connect to a dead address instead of starting fresh -- bit twice by
# this already this session).
ray.init(address="local", resources={"openpiton": 1}, log_to_driver=False)

node = OpenPitonWorkspaceNode(ROOT, pg_ready_timeout_s=120)
try:
    print(f"configuring {X_TILES}x{Y_TILES} ariane...", flush=True)
    cfg = get(node.configure.chia_remote(
        x_tiles=X_TILES, y_tiles=Y_TILES, core="ariane", extra_flags=ZICSR
    ))
    print(f"build_id={cfg.build_id} key={cfg.key}", flush=True)

    print(f"building {X_TILES}x{Y_TILES} (pivoted from 2x2 -- see module docstring), "
          f"be patient...", flush=True)
    started = time.monotonic()
    art = get(node.build.chia_remote(cfg, timeout_seconds=7200))
    wall = time.monotonic() - started
    print(f"build wall_time={wall:.0f}s success={art.success}", flush=True)
    if not art.success:
        print(f"FAILURE_REASON: {art.failure_reason}", flush=True)
        print(f"STDERR_TAIL:\n{art.stderr[-3000:]}", flush=True)
        raise SystemExit(1)
    print(f"binary_path={art.binary_path}", flush=True)
    assert os.path.exists(art.binary_path), "build reported success but binary is missing"

    # hello_world_many.c is the multi-tile-validated upstream test
    # (ariane_tile16_simple's own diag). finish_mask needs one '1' per tile.
    print(f"running hello_world_many.c on the {X_TILES}x{Y_TILES} mesh "
          f"(finish_mask={FINISH_MASK})...", flush=True)
    started = time.monotonic()
    res = get(node.run.chia_remote(
        cfg, "hello_world_many.c", finish_mask=FINISH_MASK,
        # Exactly the diaglist's own proven value for this config
        # (master_diaglist_princeton:432) -- not the value that turned out
        # insufficient/irrelevant for the abandoned 2x2 attempt.
        rtl_timeout=10_000_000, max_cycle=10_000_000, timeout_seconds=7200,
    ))
    wall = time.monotonic() - started
    print(f"run wall_time={wall:.0f}s verdict={res.verdict} success={res.success}", flush=True)
    if res.verdict != "pass":
        print(f"SIM_LOG_TAIL:\n{res.sim_log_tail[-3000:]}", flush=True)
        raise SystemExit(1)

    print(f"{X_TILES}x{Y_TILES} ARIANE MESH: PASS", flush=True)
finally:
    node.close()
    ray.shutdown()
