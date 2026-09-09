## Title
Tailnet-relayed worker never receives a task lease from the GCS (registration works, dispatch doesn't)

## Body

We're running a `chia up` cluster with a `tailnet:` section — head node behind NAT, one GCP worker joined through chia's managed Tailscale relay instead of direct addressing. Bring-up works fine: both nodes join the tailnet, `ray.nodes()` shows the GCP worker `Alive` with the right resources, `ray status` reports the combined pool correctly.

The problem shows up once we try to actually run something on that GCP worker specifically. A task whose resource request can only be satisfied there (we tried both `NodeAffinitySchedulingStrategy(node_id=<gcp>, soft=False)` and just requesting more units of a custom resource than the local node has) never runs. It just sits pending forever — no error, no timeout, nothing.

We spent a while ruling out the obvious explanations before deciding this was worth filing. It's not a worker-startup issue — `ray.nodes()` shows the GCP node registered and `Alive` the whole time, correct resource counts throughout. We also suspected the outbound gRPC-CONNECT-proxy setup chia does for tailnet nodes (`RAY_grpc_enable_http_proxy` / `grpc_proxy=http://127.0.0.1:<port>`), but that's specifically for outbound traffic, and outbound clearly works fine — that's how the node manages to register and heartbeat in the first place. Not resource contention or a slow scheduler either: we pulled the raylet's own state-dump on the GCP node while a task was sitting pending, and every relevant counter (`num PYTHON pending start requests`, `num PYTHON pending registration requests`, all the `process_failed_*` reasons) read exactly 0. The WorkerPool was never even asked to start a worker for the task — not tried-and-failed, never attempted at all.

So our best read is that whatever channel the GCS uses to push a task-lease assignment down to a specific raylet behaves differently under this tailnet-relay setup than the channel that raylet uses for its own outbound registration/heartbeats. One direction works, the other doesn't seem to reach the node.

We haven't done packet-level tracing to nail down exactly which RPC is missing — that felt like the right next step but more than we could take on ourselves right now. Filing this in case it's a known gap, or in case someone closer to the relay implementation can spot it faster than we can. Happy to share the cluster config, full logs, or a minimal repro — this reproduced consistently across several `chia up`/`chia down` cycles, not a one-off.
