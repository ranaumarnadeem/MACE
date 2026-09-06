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

Tier 2 is gated because a real build needs a checkout, a RISC-V toolchain and
Verilator. Fixtures skip rather than error when the environment is absent, per
CHIA's own convention.

The two-checkout requirement in the fan-out test is not incidental: OpenPiton's
template preprocessor writes generated .tmp.v files into the source tree on
every build, so concurrent builds must not share a checkout.
"""

from __future__ import annotations

import os
import time

import pytest

ray = pytest.importorskip("ray")

from chia.base.ChiaFunction import get  # noqa: E402

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode  # noqa: E402
from chia_openpiton.state_def import PitonConfig  # noqa: E402

REAL = os.environ.get("OPENPITON_TEST_REAL") == "1"
ROOT = os.environ.get("OPENPITON_ROOT", "")
ROOT_2 = os.environ.get("OPENPITON_ROOT_2", "")
CORE = os.environ.get("OPENPITON_TEST_CORE", "ariane")

# binutils 2.38+ split zicsr/zifencei out of base RV64I; OpenPiton's 2019 diags
# need it spelled out. sims exposes this without patching anything.
ZICSR = ("-rv64_march=rv64imafdc_zicsr_zifencei",)

real_only = pytest.mark.skipif(
    not REAL, reason="set OPENPITON_TEST_REAL=1 (and OPENPITON_ROOT) to run"
)


@pytest.fixture(scope="module")
def ray_local():
    """A local Ray with the custom resources these nodes demand.

    Without `openpiton` advertised, a placement group reservation waits
    forever rather than failing, so the count here is what bounds the
    fan-out test.
    """
    slots = 2 if ROOT_2 else 1
    ray.init(resources={"openpiton": slots}, ignore_reinit_error=True, log_to_driver=False)
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

            test = "hello_world.c" if CORE == "ariane" else "princeton-test-test.s"
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
