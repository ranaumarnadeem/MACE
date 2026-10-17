"""Regression gate for the generic tile: PicoRV32 on the generic path.

Builds core="generic" through OpenPitonWorkspaceNode (the adapter MACE uses)
on one mesh, runs PicoRV32's gate tests and the generic tile's own
programs, and checks each verdict: the passing tests must pass,
generic_expect_fail.S must fail, and generic_expect_timeout.S must time out.
It needs no LLM.

The checkout must be patched with scripts/patch_openpiton.sh (fixes 14-19
add the generic tile, its image base, and C programs), and tile/generic/rtl/Flist.core must name PicoRV32's
wrapper, which fix 14 writes when the file is missing.

Run with PITON_ROOT set to the checkout:
    PITON_ROOT=~/openpiton python scripts/local_generic_gate.py 2 2

Exits 0 when every verdict is the expected one.
"""
from __future__ import annotations

import os
import sys
import time

import ray

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

ROOT = os.environ.get("PITON_ROOT") or sys.exit("Set PITON_ROOT to a patched OpenPiton checkout.")
X, Y = (int(a) for a in sys.argv[1:3]) if len(sys.argv) >= 3 else (2, 2)
os.environ["MAKEFLAGS"] = "-j1"

EXPECTED = {
    "addi.S": "pass",
    "beq.S": "pass",
    "jal.S": "pass",
    "lw.S": "pass",
    "mul.S": "pass",
    "sb.S": "pass",
    "sh.S": "pass",
    "generic_tile_id.S": "pass",
    "generic_shared_flag.S": "pass",
    "generic_amo_counter.S": "pass",
    "generic_expect_fail.S": "fail",
    "generic_expect_timeout.S": "timeout",
}

ray.init(address="local", resources={"openpiton": 1}, log_to_driver=False)
node = OpenPitonWorkspaceNode(ROOT, pg_ready_timeout_s=120)
wrong = []
try:
    cfg = get(node.configure.chia_remote(
        x_tiles=X, y_tiles=Y, core="generic",
        config_rtl=("MINIMAL_MONITORING", "CONFIG_DISABLE_BIST_CLEAR"),
    ))
    print(f"{X}x{Y} generic: build_id={cfg.build_id}", flush=True)
    print("sims_flags:", " ".join(cfg.sims_flags()), flush=True)

    started = time.monotonic()
    art = get(node.build.chia_remote(cfg, timeout_seconds=3600))
    print(f"build: success={art.success} reused={art.reused} wall={time.monotonic() - started:.0f}s", flush=True)
    if not art.success:
        print(f"failure_reason={art.failure_reason}\n{art.stderr[-3000:]}", flush=True)
        raise SystemExit(1)

    for test, want in EXPECTED.items():
        started = time.monotonic()
        res = get(node.run.chia_remote(cfg, test, timeout_seconds=1200))
        ok = res.verdict == want
        print(f"{'ok   ' if ok else 'WRONG'} {test}: verdict={res.verdict} expected={want} "
              f"wall={time.monotonic() - started:.0f}s", flush=True)
        if not ok:
            wrong.append(test)
            print(res.sim_log_tail[-1500:], flush=True)
finally:
    node.close()
    ray.shutdown()

print(f"{len(EXPECTED) - len(wrong)} of {len(EXPECTED)} verdicts as expected", flush=True)
sys.exit(1 if wrong else 0)
