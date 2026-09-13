"""Configure 1x1 Ariane with a reduced L1D cache and build the Verilator model.

Cache geometry under test (corrected per t3 diagnosis):
    l1d = (4096, 4)   # reduced capacity, default associativity: default is (8192, 4)
    l1i = (16384, 4)  # default (unchanged)
    l15 = (8192, 4)   # default (unchanged)
    l2  = (65536, 4)  # default (unchanged)

The first probe used l1d=(4096, 2), which shrank BOTH capacity and
associativity at once. The t3 diagnosis calls for a correction that is still
strictly smaller/lower-assoc than the default (8192, 4) but reduces only ONE
dimension -- here capacity -- while keeping associativity at the default 4
(more sets per line, same way count).

`configure(caches=...)` REPLACES the cache dict, it does not merge, so every
cache is spelled out here -- changing only l1d would silently drop l1i/l15/l2
from the sims flags. The config key hashes the cache geometry, so this build
lands in its own model directory (distinct build_id) and cannot collide with
the default-geometry 1x1 Ariane model.

Run from WSL, real checkout:
    python scripts/ariane_1x1_small_l1d_build.py
"""
from __future__ import annotations

import os
import time

import ray

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

ROOT = "/mnt/c/Users/Potato/Desktop/openpiton"

# l1d capacity halved from (8192, 4) to (4096, 4) per t3 diagnosis; associativity
# stays at the default 4. Everything else at DEFAULT_CACHES.
CACHES = {
    "l1i": (16384, 4),
    "l1d": (4096, 4),
    "l15": (8192, 4),
    "l2": (65536, 4),
}

# Use -j1 to avoid OOM on memory-constrained systems. The Verilator C++
# compilation of a 1x1 Ariane design is modest but parallel cc1plus jobs
# can still spike. Safe default; remove if you have headroom.
os.environ["MAKEFLAGS"] = "-j1"

# address="local" forces a brand-new local Ray instance
ray.init(address="local", resources={"openpiton": 1}, log_to_driver=False)

node = OpenPitonWorkspaceNode(ROOT, pg_ready_timeout_s=120)
try:
    # ---- Configure: Ariane core, 1x1 mesh, custom cache geometry ----
    print("=" * 60)
    print("CONFIGURING: Ariane core, 1x1 mesh, l1d=(4096,4)")
    print("=" * 60, flush=True)
    started = time.monotonic()
    cfg = get(node.configure.chia_remote(
        x_tiles=1,
        y_tiles=1,
        core="ariane",
        network_config="2dmesh_config",
        caches=CACHES,
    ))
    elapsed = time.monotonic() - started
    print(f"Configuration completed in {elapsed:.1f}s")
    print(f"  build_id : {cfg.build_id}")
    print(f"  key      : {cfg.key[:16]}...")
    print(f"  core     : {cfg.core}")
    print(f"  mesh     : {cfg.x_tiles}x{cfg.y_tiles}")
    print(f"  network  : {cfg.network_config}")
    print(f"  caches   : {dict(sorted(cfg.caches.items()))}")
    print(f"  sims_flags: {cfg.sims_flags()}", flush=True)

    # ---- Build: Verilator RTL model ----
    print()
    print("=" * 60)
    print("BUILDING: Verilator RTL model for 1x1 Ariane (small L1D)")
    print("(This may take 15-60 minutes depending on hardware)")
    print("=" * 60, flush=True)
    started = time.monotonic()
    art = get(node.build.chia_remote(cfg, timeout_seconds=7200))
    wall = time.monotonic() - started
    print()
    print(f"Build completed in {wall:.0f}s ({wall/60:.1f} min)")
    print(f"  success         : {art.success}")
    print(f"  returncode      : {art.returncode}")
    print(f"  verilator_ver   : {art.verilator_version}")
    print(f"  model_dir       : {art.model_dir}")
    print(f"  binary_path     : {art.binary_path}")
    print(f"  reused          : {art.reused}")
    if not art.success:
        print(f"  failure_reason  : {art.failure_reason}")
        print(f"  STDERR_TAIL:\n{art.stderr[-3000:]}")
        raise SystemExit(1)

    # Verify binary exists on disk
    assert os.path.exists(art.binary_path), (
        f"build reported success but binary is missing: {art.binary_path}"
    )
    binary_size_mb = os.path.getsize(art.binary_path) / (1024 * 1024)
    print(f"  binary_size     : {binary_size_mb:.1f} MB")
    print()
    print("=" * 60)
    print("SUCCESS: 1x1 Ariane (l1d=4096x4) Verilator RTL model built!")
    print(f"  Binary: {art.binary_path}")
    print("=" * 60, flush=True)

finally:
    node.close()
    ray.shutdown()