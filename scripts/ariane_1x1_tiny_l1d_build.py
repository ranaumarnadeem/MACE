"""Configure 1x1 Ariane with a minimal L1D cache and build the Verilator model.

Cache geometry under test:
    l1d = (128, 1)    # minimal capacity AND associativity (1 direct-mapped line)
    l1i = (16384, 4)  # default (unchanged)
    l15 = (8192, 4)   # default (unchanged)
    l2  = (65536, 4)  # default (unchanged)

This is the stress-tested extreme: default l1d is (8192, 4) and the previously
probed reduction was (4096, 4) -- this config drops l1d to a single 128-byte
direct-mapped set while keeping every other cache at its default geometry.

`configure(caches=...)` REPLACES the cache dict, it does not merge, so every
cache is spelled out here -- changing only l1d would silently drop l1i/l15/l2
from the sims flags. The config key hashes the cache geometry, so this build
lands in its own model directory (distinct build_id) and cannot collide with
either the default-geometry or the (4096,4) 1x1 Ariane models.

Run from WSL, real checkout:
    python scripts/ariane_1x1_tiny_l1d_build.py
"""
from __future__ import annotations

import os
import time

import ray

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

ROOT = "/mnt/c/Users/Potato/Desktop/openpiton"

# l1d reduced to (128, 1): a single 128-byte direct-mapped set. All other
# caches at DEFAULT_CACHES.
CACHES = {
    "l1i": (16384, 4),
    "l1d": (128, 1),
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
    # ---- Configure: Ariane core, 1x1 mesh, tiny L1D geometry ----
    print("=" * 60)
    print("CONFIGURING: Ariane core, 1x1 mesh, l1d=(128,1)")
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

    # Confirm this is a distinct model directory, not a rebuild of an existing
    # geometry: the cache hash feeds build_id, and no prior build used
    # l1d=(128,1), so this dir must not exist yet.
    model_dir = os.path.join(ROOT, "build", "manycore", cfg.build_id)
    print(f"  model_dir: {model_dir}")
    print(f"  exists already : {os.path.isdir(model_dir)}", flush=True)

    # ---- Build: Verilator RTL model ----
    print()
    print("=" * 60)
    print("BUILDING: Verilator RTL model for 1x1 Ariane (l1d=128x1)")
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
    print("SUCCESS: 1x1 Ariane (l1d=128x1) Verilator RTL model built!")
    print(f"  Binary: {art.binary_path}")
    print("=" * 60, flush=True)

finally:
    node.close()
    ray.shutdown()