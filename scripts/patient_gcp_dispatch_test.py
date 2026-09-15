"""Diagnostic: does a Ray task actually execute on the GCP worker under this
cluster's tailnet-relay topology, given enough time to resolve -- and if not,
which hop actually fails?

Forces placement onto the GCP node by requesting more `openpiton` units
than the local node can ever satisfy (2 local vs 8 GCP -- see
cluster/local.yaml), so no NodeAffinitySchedulingStrategy is needed. Polls
ray.wait() every 10s instead of blocking on ray.get(), printing progress the
whole way, so this can run for several minutes without looking stuck.

Extended from the original version (which only checked the raylet's own
internal state-dump after the fact) to also capture BOTH tailnet relays' own
logs -- the head's (read directly, this driver runs on the head) and the
GCP worker's (read over SSH, same auth cluster/local.yaml itself uses) --
bracketing the dispatch window. chia_openpiton.cluster.tailnet's relay logs
every CONNECT attempt and every dial failure
(`relay: CONNECT %s:%d dial failed: %s`) to its own log file
(/tmp/chia_tailnet_relay_$USER.log), so diffing each log's content across
the dispatch window answers precisely which of the two hops --
head-relay-to-worker-tailnet-IP, or worker-tailscaled-to-raylet-port --
never happens, rather than only knowing that neither one, somehow, worked.

Run from the WSL head, cluster already up:
    python scripts/patient_gcp_dispatch_test.py
"""
from __future__ import annotations

import getpass
import os
import subprocess
import time

import ray

ray.init(address="auto", log_to_driver=False)

nodes = ray.nodes()
gcp = next(n for n in nodes if n.get("Alive") and n.get("Resources", {}).get("openpiton", 0) > 2)
print(f"GCP node_id={gcp['NodeID']} address={gcp.get('NodeManagerAddress')}", flush=True)

HEAD_RELAY_LOG = f"/tmp/chia_tailnet_relay_{getpass.getuser()}.log"


def _read_or_empty(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _gcp_worker_ip() -> str | None:
    """The GCP worker's real SSH-reachable IP, the same way `chia up` finds
    it -- gcloud, not Ray's own tailnet/advertise addressing."""
    try:
        out = subprocess.run(
            ["gcloud", "compute", "instances", "list",
             "--filter=status=RUNNING", "--format=value(EXTERNAL_IP)"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        ip = out.stdout.strip().splitlines()
        return ip[0] if ip else None
    except Exception as e:  # noqa: BLE001
        print(f"could not determine GCP worker IP via gcloud: {e}", flush=True)
        return None


def _read_worker_relay_log(worker_ip: str, ssh_key: str) -> str:
    try:
        out = subprocess.run(
            ["ssh", "-i", ssh_key, "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=10", f"chia@{worker_ip}",
             "cat /tmp/chia_tailnet_relay_chia.log 2>/dev/null || true"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout
    except Exception as e:  # noqa: BLE001
        return f"(could not read worker relay log: {e})"


worker_ip = _gcp_worker_ip()
ssh_key = os.path.expanduser(os.environ.get("GCP_SSH_KEY", "~/.ssh/id_ed25519_gcp_chia"))
print(f"GCP worker SSH IP: {worker_ip}", flush=True)

head_log_before = _read_or_empty(HEAD_RELAY_LOG)
worker_log_before = _read_worker_relay_log(worker_ip, ssh_key) if worker_ip else ""
print(f"baseline: head relay log {len(head_log_before)}B, worker relay log {len(worker_log_before)}B", flush=True)


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

head_log_after = _read_or_empty(HEAD_RELAY_LOG)
worker_log_after = _read_worker_relay_log(worker_ip, ssh_key) if worker_ip else ""

print("\n=== head relay log, new lines during this dispatch ===", flush=True)
new_head = head_log_after[len(head_log_before):]
print(new_head if new_head.strip() else "(nothing new -- head relay logged zero activity for this dispatch)", flush=True)

print("\n=== GCP worker relay log, new lines during this dispatch ===", flush=True)
new_worker = worker_log_after[len(worker_log_before):]
print(new_worker if new_worker.strip() else "(nothing new -- worker relay logged zero activity for this dispatch)", flush=True)

ray.shutdown()
