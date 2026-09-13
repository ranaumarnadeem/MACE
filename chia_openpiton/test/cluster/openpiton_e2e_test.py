"""Tier-1/2 tests: real Ray, and (opt-in) a real OpenPiton checkout.

Run:
    # tier 1 -- live Ray, stub sims, no OpenPiton needed
    pytest chia_openpiton/test/cluster/openpiton_e2e_test.py -q

    # tier 2 -- the real thing (acceptance tests 1 and 3)
    OPENPITON_TEST_REAL=1 \
    OPENPITON_ROOT=/home/you/openpiton \
    OPENPITON_ROOT_2=/home/you/openpiton-b \
    RISCV=/home/you/scratch/riscv_install \
    pytest chia_openpiton/test/cluster/openpiton_e2e_test.py -q -s

    # tier 2, GCP -- acceptance test 2, against an already-running
    # `chia up cluster/local.yaml` cluster (head + a real GCP worker)
    OPENPITON_TEST_GCP=1 \
    OPENPITON_GCP_ROOT=/home/chia/openpiton \
    pytest chia_openpiton/test/cluster/openpiton_e2e_test.py -q -s -k Acceptance2

Tier 2 is gated because a real build needs a checkout, a RISC-V toolchain and
Verilator. Fixtures skip rather than error when the environment is absent, per
CHIA's own convention.

The two-checkout requirement in the fan-out test is not incidental: OpenPiton's
template preprocessor writes generated .tmp.v files into the source tree on
every build, so concurrent builds must not share a checkout.

Acceptance test 2 (GCP) must run from the cluster's head node (this attaches
to the live cluster via ``ray.init(address="auto")`` rather than creating a
fresh local one) and pins every dispatch to the GCP worker's node_id by hand
-- see TestAcceptance2's docstring for why OpenPitonWorkspaceNode's normal
placement-group pinning is not enough by itself on a multi-machine cluster.
"""

from __future__ import annotations

import os
import time

import pytest

ray = pytest.importorskip("ray")

from chia.base.ChiaFunction import get  # noqa: E402
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy  # noqa: E402

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode  # noqa: E402
from chia_openpiton.state_def import PitonConfig  # noqa: E402

REAL = os.environ.get("OPENPITON_TEST_REAL") == "1"
ROOT = os.environ.get("OPENPITON_ROOT", "")
ROOT_2 = os.environ.get("OPENPITON_ROOT_2", "")
CORE = os.environ.get("OPENPITON_TEST_CORE", "ariane")
GCP = os.environ.get("OPENPITON_TEST_GCP") == "1"
GCP_ROOT = os.environ.get("OPENPITON_GCP_ROOT", "")

# binutils 2.38+ split zicsr/zifencei out of base RV64I; OpenPiton's 2019 diags
# need it spelled out. sims exposes this without patching anything.
ZICSR = ("-rv64_march=rv64imafdc_zicsr_zifencei",)

real_only = pytest.mark.skipif(
    not REAL, reason="set OPENPITON_TEST_REAL=1 (and OPENPITON_ROOT) to run"
)
gcp_only = pytest.mark.skipif(
    not GCP,
    reason="set OPENPITON_TEST_GCP=1 (and OPENPITON_GCP_ROOT) against a live "
    "'chia up cluster/local.yaml' cluster",
)


@pytest.fixture(scope="module")
def ray_local():
    """A local Ray with the custom resources these nodes demand.

    Without `openpiton` advertised, a placement group reservation waits
    forever rather than failing, so the count here is what bounds the
    fan-out test.
    """
    slots = 2 if ROOT_2 else 1
    # address="local": forces a fresh local instance regardless of any stale
    # /tmp/ray/ray_current_cluster marker from an earlier torn-down cluster.
    ray.init(
        address="local",
        resources={"openpiton": slots},
        ignore_reinit_error=True,
        log_to_driver=False,
    )
    yield slots
    ray.shutdown()


@pytest.fixture(scope="module")
def checkout():
    if not REAL:
        pytest.skip("tier 2 not enabled")
    if not ROOT or not os.path.isdir(ROOT):
        pytest.skip(f"OPENPITON_ROOT not a directory: {ROOT!r}")
    return ROOT


class TestPlacement:
    """Tier 1: real dispatch and placement, stub-free but tool-free too."""

    def test_node_reserves_and_releases_its_bundle(self, ray_local):
        node = OpenPitonWorkspaceNode(os.getcwd(), pg_ready_timeout_s=60)
        try:
            assert node.placement_group is not None
            assert node.owns_placement_group is True
        finally:
            node.close()
        assert node.placement_group is None

    def test_close_is_idempotent(self, ray_local):
        node = OpenPitonWorkspaceNode(os.getcwd(), pg_ready_timeout_s=60)
        node.close()
        node.close()  # must not raise

    def test_unpinned_node_has_no_bundle(self, ray_local):
        node = OpenPitonWorkspaceNode(os.getcwd(), require_colocated=False)
        assert node.placement_group is None
        assert node.task_options == {}


@real_only
class TestAcceptance1:
    """configure -> build -> run, verdict from the transcript."""

    def test_build_and_run_hello_world(self, ray_local, checkout):
        node = OpenPitonWorkspaceNode(checkout, pg_ready_timeout_s=120)
        try:
            cfg = get(node.configure.chia_remote(
                x_tiles=1, y_tiles=1, core=CORE, extra_flags=ZICSR))
            assert cfg.build_id.startswith("mace_")

            art = get(node.build.chia_remote(cfg, timeout_seconds=5400))
            assert art.success, f"build failed ({art.failure_reason}): {art.stderr[-1500:]}"
            assert os.path.exists(art.binary_path)

            test = (
                "hello_world.c" if CORE == "ariane"
                else "addi.S" if CORE == "pico"
                else "princeton-test-test.s"
            )
            res = get(node.run.chia_remote(
                cfg, test, rtl_timeout=1000000, timeout_seconds=1800))
            assert res.verdict == "pass", f"verdict={res.verdict}: {res.sim_log_tail[-1500:]}"
            assert res.success is True
            # The verdict line carries $time; status.log has no Cyc= here.
            assert res.sim_time and res.sim_time > 0
        finally:
            node.close()

    def test_rebuild_of_an_identical_config_reuses_the_model_dir(self, ray_local, checkout):
        """Same config -> same build_id, so no second model directory appears."""
        node = OpenPitonWorkspaceNode(checkout, pg_ready_timeout_s=120)
        try:
            a = get(node.configure.chia_remote(x_tiles=1, y_tiles=1, core=CORE,
                                               extra_flags=ZICSR))
            b = get(node.configure.chia_remote(x_tiles=1, y_tiles=1, core=CORE,
                                               extra_flags=ZICSR))
            assert a.key == b.key
            assert a.build_id == b.build_id
        finally:
            node.close()


@real_only
@pytest.mark.skipif(not ROOT_2, reason="set OPENPITON_ROOT_2 to a second checkout")
class TestAcceptance3:
    """Two builds in parallel, one per checkout."""

    def test_parallel_builds_across_two_checkouts(self, ray_local, checkout):
        assert ray_local >= 2, "needs two openpiton slots"
        nodes = [
            OpenPitonWorkspaceNode(checkout, pg_ready_timeout_s=120),
            OpenPitonWorkspaceNode(ROOT_2, pg_ready_timeout_s=120),
        ]
        try:
            # Different meshes so neither can be served by the other's model.
            cfgs = [
                get(n.configure.chia_remote(x_tiles=x, y_tiles=1, core=CORE,
                                            extra_flags=ZICSR))
                for n, x in zip(nodes, (1, 2))
            ]
            assert cfgs[0].build_id != cfgs[1].build_id

            started = time.time()
            refs = [n.build.chia_remote(c, timeout_seconds=5400)
                    for n, c in zip(nodes, cfgs)]
            arts = [get(r) for r in refs]
            wall = time.time() - started

            for art in arts:
                assert art.success, f"{art.failure_reason}: {art.stderr[-800:]}"
            # Genuinely concurrent: the wall clock must beat running them back
            # to back, with slack for scheduling and the shared toolchain.
            serial = sum(a.wall_time_s for a in arts)
            print(f"\nparallel={wall:.0f}s serial-equivalent={serial:.0f}s")
            assert wall < serial * 0.9, "builds did not overlap"
        finally:
            for n in nodes:
                n.close()


@pytest.fixture(scope="module")
def ray_cluster():
    """Attach to an already-running `chia up` cluster (head + GCP worker).

    Unlike ray_local, this does not create a fresh local Ray -- it connects
    to real multi-machine infrastructure that must already be up (this test
    is meant to run from the cluster's head node). No ray.shutdown() on
    teardown: this cluster belongs to `chia down`, not to this fixture.
    """
    if not GCP:
        pytest.skip("tier-2-GCP not enabled")
    ray.init(address="auto", ignore_reinit_error=True, log_to_driver=False)
    yield


@pytest.fixture(scope="module")
def gcp_pin(ray_cluster):
    """NodeAffinitySchedulingStrategy pinned to the GCP worker.

    OpenPitonWorkspaceNode's own placement-group pinning (ColocatedNode)
    reserves resource *shape*, not a specific machine. This cluster's two
    node types both advertise the same "openpiton" resource name
    (cluster/local.yaml: openpiton_local -> 2 slots on this WSL head,
    openpiton_gcp -> 8 slots on a real GCP VM with a different filesystem
    entirely) -- a vanilla PG bundle request for {"openpiton": 1} can
    legally land on either. Matched here by resource count (8 vs 2) rather
    than by IP: chia's tailnet relay renumbers node-manager addresses, so
    IP matching would be fragile in a way resource count isn't.
    """
    candidates = [
        n for n in ray.nodes()
        if n.get("Alive") and n.get("Resources", {}).get("openpiton", 0) > 2
    ]
    if not candidates:
        pytest.skip("no live Ray node advertising >2 openpiton slots (GCP worker not up?)")
    return NodeAffinitySchedulingStrategy(node_id=candidates[0]["NodeID"], soft=False)


@pytest.fixture(scope="module")
def gcp_checkout():
    if not GCP_ROOT:
        pytest.skip("set OPENPITON_GCP_ROOT to the checkout path on the GCP worker")
    return GCP_ROOT


@gcp_only
class TestAcceptance2:
    """2x2 Ariane build+run on a real GCP worker.

    Phase 1's long-deferred acceptance test: the same configure -> build ->
    run shape as TestAcceptance1, but against real GCP compute instead of
    this WSL host, and pinned to that specific worker by hand (see gcp_pin)
    rather than trusting OpenPitonWorkspaceNode's default placement-group
    reservation, which cannot tell this cluster's two same-named
    "openpiton" pools apart.

    Uses hello_world_many.c (not hello_world.c) and a matching finish_mask:
    hello_world.c is only Verilator-validated upstream for a single tile
    (ariane_tile1_simple); hello_world_many.c is the multi-tile-validated
    counterpart (ariane_tile16_simple), which is what a 2x2 mesh needs.
    """

    def test_2x2_build_and_run_on_gcp(self, gcp_pin, gcp_checkout):
        node = OpenPitonWorkspaceNode(
            gcp_checkout, require_colocated=False, root_on_remote_worker=True
        )
        try:
            cfg = get(node.configure.options(scheduling_strategy=gcp_pin).chia_remote(
                x_tiles=2, y_tiles=2, core="ariane", extra_flags=ZICSR))
            assert cfg.build_id.startswith("mace_")

            art = get(node.build.options(scheduling_strategy=gcp_pin).chia_remote(
                cfg, timeout_seconds=5400))
            assert art.success, f"build failed ({art.failure_reason}): {art.stderr[-1500:]}"
            assert art.binary_path

            res = get(node.run.options(scheduling_strategy=gcp_pin).chia_remote(
                cfg, "hello_world_many.c", finish_mask="1111",
                rtl_timeout=10_000_000, timeout_seconds=1800))
            assert res.verdict == "pass", f"verdict={res.verdict}: {res.sim_log_tail[-1500:]}"
            assert res.success is True
        finally:
            node.close()
