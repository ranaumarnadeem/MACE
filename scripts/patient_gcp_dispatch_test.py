"""One-off diagnostic: does a Ray task actually execute on the GCP worker
under this cluster's tailnet-relay topology, given enough time to resolve?

Forces placement onto the GCP node by requesting more `openpiton` units
than the local node can ever satisfy (2 local vs 8 GCP -- see
cluster/local.yaml), so no NodeAffinitySchedulingStrategy is needed. Polls
ray.wait() every 10s instead of blocking on ray.get(), printing progress the
whole way, so this can run for several minutes without looking stuck.

Run from the WSL head, cluster already up:
    python scripts/patient_gcp_dispatch_test.py
"""
from __future__ import annotations

import time

import ray

ray.init(address="auto", log_to_driver=False)

nodes = ray.nodes()
gcp = next(n for n in nodes if n.get("Alive") and n.get("Resources", {}).get("openpiton", 0) > 2)
print(f"GCP node_id={gcp['NodeID']} address={gcp.get('NodeManagerAddress')}", flush=True)


@ray.remote(resources={"openpiton": 3})
def where():
    import os
    import socket

    return {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "grpc_proxy": os.environ.get("grpc_proxy"),
        "RAY_grpc_enable_http_proxy": os.environ.get("RAY_grpc_enable_http_proxy"),
        "no_grpc_proxy": os.environ.get("no_grpc_proxy"),
    }


print("dispatching where() forced to the GCP pool (resources={'openpiton': 3})...", flush=True)
started = time.monotonic()
ref = where.remote()

deadline_s = 360  # 6 minutes -- do not give up early
poll_s = 10
while True:
    elapsed = time.monotonic() - started
    ready, pending = ray.wait([ref], timeout=poll_s)
    elapsed = time.monotonic() - started
    if ready:
        print(f"[{elapsed:6.1f}s] READY", flush=True)
        try:
            result = ray.get(ready[0])
            print(f"RESULT: {result}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"EXCEPTION: {type(e).__name__}: {e}", flush=True)
        break
    print(f"[{elapsed:6.1f}s] still pending...", flush=True)
    if elapsed >= deadline_s:
        print(f"[{elapsed:6.1f}s] giving up after {deadline_s}s -- genuinely stuck", flush=True)
        break

ray.shutdown()
