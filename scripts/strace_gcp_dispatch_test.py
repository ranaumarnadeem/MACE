"""Decisive diagnostic: does the head's raylet even attempt a connect()
toward the GCP worker during a stuck dispatch, or does it never try at all?

scripts/patient_gcp_dispatch_test.py already proved (2026-09-16, real
cluster) that neither tailnet relay logs any CONNECT activity during a
360s-stuck dispatch -- ruling out chia's relay code, which is never asked
to do anything. That leaves two possibilities the relay-log approach can't
tell apart: the head's raylet never attempts an outbound connection at all
(consistent with Ray's raylet-to-raylet lease RPC not being grpc_proxy-aware
in its compiled C++ core, a different code path from Python-level calls),
or it does attempt one and something below the relay silently swallows it.

strace on both raylets during the same forced-placement dispatch answers
this directly: a `connect()` syscall naming the worker's real tailnet IP or
the local relay's CONNECT proxy port (127.0.0.1:<connect_proxy_port>) means
the attempt happens and dies somewhere else; its complete absence means the
raylet's own lease-request code never tries -- squarely a Ray-internals
question, not anything further chia-config reading will fix.

Needs strace on both the head (this machine) and the GCP worker -- installs
it on the worker if missing, assumes it's already on the head (apt-get
install -y strace, needs the user's own sudo password, not run from here).

Run from the WSL head, cluster already up:
    python scripts/strace_gcp_dispatch_test.py
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time

import ray
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

ray.init(address="auto", log_to_driver=False)

# The GCP node is the live node that advertises openpiton from a host other
# than this one; gcp_pin in chia_openpiton/test/cluster/openpiton_e2e_test.py
# gives the reasons.
head_host = socket.gethostname()
nodes = ray.nodes()
gcp = next(n for n in nodes if n.get("Alive")
           and n.get("Resources", {}).get("openpiton", 0) > 0
           and n.get("NodeManagerHostname") != head_host)
print(f"GCP node_id={gcp['NodeID']} host={gcp.get('NodeManagerHostname')} "
      f"address={gcp.get('NodeManagerAddress')}", flush=True)

HEAD_TRACE_LOG = "/tmp/chia_head_raylet_strace.log"
WORKER_TRACE_LOG = "/tmp/chia_worker_raylet_strace.log"


def _gcp_worker_ip() -> str:
    out = subprocess.run(
        ["gcloud", "compute", "instances", "list",
         "--filter=status=RUNNING", "--format=value(EXTERNAL_IP)"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return out.stdout.strip().splitlines()[0]


def _ssh(worker_ip: str, ssh_key: str, remote_cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", ssh_key, "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=10", f"chia@{worker_ip}", remote_cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _find_local_raylet_pid() -> int:
    out = subprocess.run(["pgrep", "-f", "-o", r"/raylet\b"], capture_output=True, text=True)
    pids = out.stdout.split()
    if not pids:
        raise RuntimeError("no local raylet process found -- is the cluster up?")
    return int(pids[0])


worker_ip = _gcp_worker_ip()
ssh_key = os.path.expanduser(os.environ.get("GCP_SSH_KEY", "~/.ssh/id_ed25519_gcp_chia"))
print(f"GCP worker SSH IP: {worker_ip}", flush=True)

print("ensuring strace is on the GCP worker...", flush=True)
_ssh(worker_ip, ssh_key, "which strace || sudo apt-get install -y -qq strace", timeout=60)

head_raylet_pid = _find_local_raylet_pid()
print(f"head raylet pid={head_raylet_pid}", flush=True)

worker_pid_out = _ssh(worker_ip, ssh_key, r"pgrep -f -o '/raylet\b'")
worker_raylet_pid = worker_pid_out.stdout.strip()
if not worker_raylet_pid:
    raise RuntimeError(f"no worker raylet process found: {worker_pid_out.stdout!r} {worker_pid_out.stderr!r}")
print(f"worker raylet pid={worker_raylet_pid}", flush=True)

# Head-side strace needs a sudo password this driver has no way to supply
# non-interactively (confirmed: the credential cache does not carry over
# to a separate invocation) -- the user runs it themselves, in parallel,
# in their own terminal:
#   sudo timeout 130 strace -f -tt -e trace=network -p <head_raylet_pid> -o /tmp/chia_head_raylet_strace.log
# This driver only starts the worker-side trace (the GCP worker's default
# cloud-init user has passwordless sudo) and the dispatch, then reads the
# head's log back once its own window has passed.
print("(head-side strace is run by the user in their own terminal -- not started here)", flush=True)

print("starting strace on worker raylet (over SSH, backgrounded remotely)...", flush=True)
_ssh(worker_ip, ssh_key,
     f"rm -f {WORKER_TRACE_LOG}; "
     f"nohup sudo strace -f -tt -e trace=network -p {worker_raylet_pid} "
     f"-o {WORKER_TRACE_LOG} > /dev/null 2>&1 & echo started")
time.sleep(2)


@ray.remote(resources={"openpiton": 1})
def where():
    import socket
    return socket.gethostname()


# Under hard node affinity, Ray 2.54's core worker sends the lease request
# from this driver straight to the GCP raylet (LocalityAwareLeasePolicy).
# The head's raylets never receive it. The 2026-09-16 run asked for 3 units
# instead, so its request went to the driver's local raylet first, which
# spilled it back to the GCP raylet.
pin = NodeAffinitySchedulingStrategy(node_id=gcp["NodeID"], soft=False)
print("dispatching where() pinned to the GCP node...", flush=True)
started = time.monotonic()
ref = where.options(scheduling_strategy=pin).remote()

TRACE_WINDOW_S = 105  # under the user's own `timeout 130` budget for the head-side trace
ready, pending = ray.wait([ref], timeout=TRACE_WINDOW_S)
elapsed = time.monotonic() - started
if ready:
    print(f"[{elapsed:6.1f}s] READY -- unexpected, it resolved this time: {ray.get(ready[0])}", flush=True)
else:
    print(f"[{elapsed:6.1f}s] still pending after {TRACE_WINDOW_S}s trace window (expected -- stopping traces now)", flush=True)

print("stopping strace on worker...", flush=True)
_ssh(worker_ip, ssh_key, "sudo pkill -INT -f 'strace.*raylet' || true")
time.sleep(2)

print("reading head trace (user's own `sudo timeout 130 strace ...` should be done or finishing)...", flush=True)
try:
    with open(HEAD_TRACE_LOG) as f:
        head_trace = f.read()
except PermissionError:
    print(f"cannot read {HEAD_TRACE_LOG} directly (root-owned) -- "
          f"run `sudo chmod 644 {HEAD_TRACE_LOG}` and re-run this read, "
          f"or `sudo cat {HEAD_TRACE_LOG}` yourself.", flush=True)
    head_trace = ""
except FileNotFoundError:
    print(f"{HEAD_TRACE_LOG} does not exist yet -- did the user's strace command finish writing it?", flush=True)
    head_trace = ""
worker_trace_out = _ssh(worker_ip, ssh_key, f"cat {WORKER_TRACE_LOG} 2>/dev/null || echo NO_FILE")
worker_trace = worker_trace_out.stdout

print(f"\nhead trace: {len(head_trace)} bytes, {head_trace.count(chr(10))} lines", flush=True)
print(f"worker trace: {len(worker_trace)} bytes, {worker_trace.count(chr(10))} lines", flush=True)

connect_re = re.compile(r"connect\(.*")
print("\n=== head raylet: every connect() during the trace window ===", flush=True)
head_connects = [l for l in head_trace.splitlines() if connect_re.search(l)]
print("\n".join(head_connects) if head_connects else "(zero connect() syscalls at all)", flush=True)

print("\n=== worker raylet: every connect() during the trace window ===", flush=True)
worker_connects = [l for l in worker_trace.splitlines() if connect_re.search(l)]
print("\n".join(worker_connects) if worker_connects else "(zero connect() syscalls at all)", flush=True)

# Full traces saved locally for anything the connect()-only filter misses.
with open("/tmp/head_raylet_strace_full.log", "w") as f:
    f.write(head_trace)
with open("/tmp/worker_raylet_strace_full.log", "w") as f:
    f.write(worker_trace)
print("\nfull traces saved to /tmp/head_raylet_strace_full.log and /tmp/worker_raylet_strace_full.log", flush=True)

ray.shutdown()
