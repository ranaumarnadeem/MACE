## Title
Tailnet-relayed worker never receives a task lease from the GCS (registration works, dispatch doesn't)

## Body

**Setup:** a `chia up` cluster with a `tailnet:` section — a head node behind NAT (no public/stable address reachable from the cloud side) plus one cloud worker (GCP, `gcp_nodes`), joined via chia's own managed Tailscale relay rather than direct addressing. Bring-up succeeds cleanly: both nodes join the tailnet, `ray.nodes()` shows the GCP worker `Alive` with the correct advertised resources, and `ray status` reports the combined resource pool correctly.

**Symptom:** a task whose resource request can *only* be satisfied by the GCP worker (either via `NodeAffinitySchedulingStrategy(node_id=<gcp>, soft=False)`, or more simply by requesting more units of a custom resource than the local node advertises) never executes. It sits pending indefinitely — no timeout, no error, just never resolves.

**What we ruled out before concluding this is a real gap, not a config mistake:**
- Not a worker-startup problem: `ray.nodes()` confirms the GCP node registers and stays `Alive` throughout, with correct resource counts, for the entire test window.
- Not the outbound gRPC-CONNECT-proxy plumbing chia sets up for tailnet nodes (`RAY_grpc_enable_http_proxy=1` / `grpc_proxy=http://127.0.0.1:<port>`) — we initially suspected this, then found and ruled it out: it's specifically for outbound traffic, and the node's own outbound registration/heartbeat traffic to the GCS clearly works (that's how it shows `Alive` at all).
- Not resource contention or a slow scheduler: checked the raylet's own state-dump on the GCP node *during* the pending window. Every relevant counter — `num PYTHON pending start requests`, `num PYTHON pending registration requests`, and every `process_failed_*` reason (`job_config_missing`, `rate_limited`, `pending_registration`, `runtime_env_setup_failed`) — reads exactly `0`. The raylet's WorkerPool was never asked to start a worker for the task at all. Not "tried and failed" — never attempted.

**Our read:** whatever channel the GCS uses to push a task-lease *assignment* to a specific raylet behaves differently under this tailnet-relay topology than the channel that raylet's own outbound registration/heartbeat uses — the first direction works, the second doesn't reach the target node.

We haven't done packet-level tracing to pin down exactly which RPC is being dropped (or never sent) — that felt like the right next step but is more than we could take on ourselves right now. Filing this in case it's a known gap, or in case someone with more context on the relay implementation can tell at a glance where to look.

Happy to share the exact cluster config, full logs, or a minimal repro if useful — this was reproduced consistently across several `chia up`/`chia down` cycles, not a one-off.
