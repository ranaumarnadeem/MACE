"""Diagnostic: what does the REAL build environment (as OpenPitonWorkspaceNode
constructs it via _env_prefix/_run) actually look like -- dispatched as a real
Ray task so it runs in the same kind of worker process the failing bootrom
build runs in, rather than a hand-reconstructed guess in an interactive shell.
"""
from __future__ import annotations

import os
import sys

import ray

ROOT = os.environ.get("PITON_ROOT") or sys.exit("Set PITON_ROOT to a patched OpenPiton checkout.")

ray.init(address="local", resources={"openpiton": 1}, log_to_driver=False)


@ray.remote(resources={"openpiton": 1})
def diag(piton_root: str) -> tuple[str, str, int]:
    from chia_openpiton.openpiton_workspace import _run

    cmd = (
        'cd piton/design/chipset/rv64_platform/bootrom/linux && '
        'make clean && make all MAX_HARTS=4; echo "make_exit=$?"'
    )
    out, err, rc, _wall = _run(cmd, piton_root, "ariane", piton_root, 60)
    return out, err, rc


out, err, rc = ray.get(diag.remote(ROOT))
print("RETURNCODE:", rc)
print("STDOUT:\n", out)
print("STDERR:\n", err)
ray.shutdown()
