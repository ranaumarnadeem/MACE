"""Tier-1/2 tests: real Ray dispatch for mace.integrator.integrate_parallel.

Run:
    # tier 1 -- live Ray, stub sims, no OpenPiton needed
    pytest mace/test/cluster/integrator_e2e_test.py -q

    # tier 2 -- the real thing, two real checkouts
    OPENPITON_TEST_REAL=1 \
    OPENPITON_ROOT=/home/you/openpiton \
    OPENPITON_ROOT_2=/home/you/openpiton-b \
    pytest mace/test/cluster/integrator_e2e_test.py -q -s

Mirrors chia_openpiton/test/cluster/openpiton_e2e_test.py's TestAcceptance3
exactly (two checkouts, real placement-group-bound dispatch), plus the
"fake_creds" resource FakeLLM.prompt needs to be remotely dispatchable.
"""

from __future__ import annotations

import os
import stat
import time

import pytest

ray = pytest.importorskip("ray")

from chia_openpiton.test.conftest import STUB_SETTINGS, STUB_SIMS  # noqa: E402

import mace.integrator as integrator_mod  # noqa: E402
from mace.integrator import integrate_parallel  # noqa: E402
from mace.spec import MaceSpec, Task  # noqa: E402
from mace.test.conftest import FakeLLM  # noqa: E402

REAL = os.environ.get("OPENPITON_TEST_REAL") == "1"
ROOT = os.environ.get("OPENPITON_ROOT", "")
ROOT_2 = os.environ.get("OPENPITON_ROOT_2", "")

real_only = pytest.mark.skipif(
    not REAL, reason="set OPENPITON_TEST_REAL=1 (and OPENPITON_ROOT/_2) to run"
)


@pytest.fixture(scope="module")
def ray_local():
    # address="local": forces a fresh local instance regardless of any stale
    # /tmp/ray/ray_current_cluster marker from an earlier torn-down cluster.
    ray.init(
        address="local",
        resources={"openpiton": 3, "fake_creds": 3},
        ignore_reinit_error=True,
        log_to_driver=False,
    )
    yield
    ray.shutdown()


def _make_stub_checkout(root, verdict: str = "pass") -> str:
    """A throwaway OpenPiton checkout, independent of conftest's tmp_path-bound
    fixture so this file can build as many as one test needs.

    The verdict is baked directly into the stub sims script's own text
    (an `export` line ahead of STUB_SIMS's body) rather than relying on
    monkeypatch.setenv("FAKE_SIMS_VERDICT", ...): the stub actually runs
    inside a Ray worker process here, which does not inherit env vars the
    test driver process sets after Ray has started. The script file itself
    is on the shared filesystem, so this works regardless of which process
    invokes it.
    """
    tools_bin = root / "piton" / "tools" / "bin"
    tools_bin.mkdir(parents=True)
    (root / "build").mkdir()
    sims = tools_bin / "sims"
    shebang, _, body = STUB_SIMS.partition("\n")
    sims.write_text(f"{shebang}\nexport FAKE_SIMS_VERDICT={verdict}\n{body}")
    sims.chmod(sims.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (root / "piton" / "piton_settings.bash").write_text(STUB_SETTINGS)
    return str(root)


@pytest.fixture
def stub_checkouts(tmp_path):
    return tuple(_make_stub_checkout(tmp_path / f"openpiton_{i}") for i in range(2))


@pytest.fixture
def failing_stub_checkouts(tmp_path):
    return tuple(
        _make_stub_checkout(tmp_path / f"openpiton_fail_{i}", verdict="fail") for i in range(2)
    )


def make_spec(**override):
    kwargs = {"workloads": ("hello_world.c",), "objective": "bring up 1x1 ariane"}
    kwargs.update(override)
    return MaceSpec(**kwargs)


def task(id, deps=(), kind="workload", spec="hello_world.c"):
    return Task(id=id, deps=deps, kind=kind, spec=spec)


class TestParallelDispatch:
    """Tier 1: real Ray, real placement groups, stub sims -- proves the fan-out
    machinery itself (batching, level ordering, fail-fast), not the adapter."""

    def test_independent_tasks_all_pass(self, ray_local, stub_checkouts):
        tasks = (task("a"), task("b"))
        llm = FakeLLM(responses=["edit a", "edit b"])

        results = integrate_parallel(stub_checkouts, make_spec(), tasks, llm)

        assert [r.task.id for r in results] == ["a", "b"]
        assert all(r.passed for r in results)

    def test_more_tasks_than_checkouts_batches(self, ray_local, stub_checkouts):
        """3 independent tasks, 2 checkouts -- one batch of 2, then a batch of 1."""
        tasks = (task("a"), task("b"), task("c"))
        llm = FakeLLM(responses=["edit a", "edit b", "edit c"])

        results = integrate_parallel(stub_checkouts, make_spec(), tasks, llm)

        assert [r.task.id for r in results] == ["a", "b", "c"]
        assert all(r.passed for r in results)

    def test_dependent_tasks_run_in_separate_levels(self, ray_local, stub_checkouts):
        tasks = (task("cfg"), task("run_it", deps=("cfg",), kind="workload"))
        llm = FakeLLM(responses=["edit cfg", "edit run_it"])

        results = integrate_parallel(stub_checkouts, make_spec(), tasks, llm)

        assert [r.task.id for r in results] == ["cfg", "run_it"]
        assert all(r.passed for r in results)

    def test_stops_at_first_failing_level(self, ray_local, failing_stub_checkouts):
        tasks = (
            task("a"),
            task("b"),
            task("c", deps=("a", "b")),
        )
        llm = FakeLLM(responses=["edit a", "edit b", "edit c"])

        results = integrate_parallel(failing_stub_checkouts, make_spec(), tasks, llm)

        # level 0 (a, b) both fail their gate -> level 1 (c) never runs.
        assert [r.task.id for r in results] == ["a", "b"]
        assert all(not r.passed for r in results)


class TestReplayTagging:
    """Proves integrate_parallel's run_id/iteration params actually reach
    mace.replay.tag_for with the expected shape. mace.replay's own
    cache/bypass round-trip (a tagged call auto-caches, a later call with
    that tag is served from cache) is proven separately in
    mace/test/cluster/replay_e2e_test.py -- this only proves the wiring
    between integrate_parallel and tag_for, via a spy rather than a real
    cache actor, since that's the part this test actually owns.
    """

    def test_run_id_given_tags_every_phase_of_every_task(
        self, ray_local, stub_checkouts, monkeypatch
    ):
        calls = []
        real_tag_for = integrator_mod.tag_for

        def _spy(run_id, iteration, task_id, phase):
            calls.append((run_id, iteration, task_id, phase))
            return real_tag_for(run_id, iteration, task_id, phase)

        monkeypatch.setattr(integrator_mod, "tag_for", _spy)

        tasks = (task("a"), task("b"))
        llm = FakeLLM(responses=["edit a", "edit b"])

        results = integrate_parallel(
            stub_checkouts, make_spec(), tasks, llm, run_id="run7", iteration=3
        )

        assert all(r.passed for r in results)
        assert set(calls) == {
            ("run7", 3, "a", "prompt"), ("run7", 3, "a", "build"), ("run7", 3, "a", "run"),
            ("run7", 3, "b", "prompt"), ("run7", 3, "b", "build"), ("run7", 3, "b", "run"),
        }
        # No wasted computation: exactly one tag_for call per (task, phase).
        assert len(calls) == 6

    def test_run_id_omitted_never_tags_anything(self, ray_local, stub_checkouts, monkeypatch):
        calls = []
        monkeypatch.setattr(integrator_mod, "tag_for", lambda *a: calls.append(a))

        tasks = (task("a"),)
        llm = FakeLLM(responses=["edit a"])

        results = integrate_parallel(stub_checkouts, make_spec(), tasks, llm)

        assert all(r.passed for r in results)
        assert calls == []

    def test_a_failed_build_still_tags_only_dispatched_phases(
        self, ray_local, failing_stub_checkouts, monkeypatch
    ):
        """A failed build means run() never dispatches (integrate_parallel's
        own if build.success filter) -- so its "run" tag must never be
        computed either, not just never used."""
        calls = []
        real_tag_for = integrator_mod.tag_for

        def _spy(run_id, iteration, task_id, phase):
            calls.append((run_id, iteration, task_id, phase))
            return real_tag_for(run_id, iteration, task_id, phase)

        monkeypatch.setattr(integrator_mod, "tag_for", _spy)

        tasks = (task("a"),)
        llm = FakeLLM(responses=["edit a"])

        integrate_parallel(
            failing_stub_checkouts, make_spec(), tasks, llm, run_id="run7", iteration=0
        )

        # verdict="fail" checkouts still build successfully (the stub sims'
        # build step always succeeds; only the run verdict is "fail" -- see
        # STUB_SIMS), so run() *does* dispatch and get tagged here too.
        assert set(calls) == {
            ("run7", 0, "a", "prompt"), ("run7", 0, "a", "build"), ("run7", 0, "a", "run"),
        }


@pytest.fixture(scope="module")
def real_checkouts():
    if not REAL:
        pytest.skip("tier 2 not enabled")
    for root in (ROOT, ROOT_2):
        if not root or not os.path.isdir(root):
            pytest.skip(f"OPENPITON_ROOT/_2 not a directory: {root!r}")
    return (ROOT, ROOT_2)


@real_only
class TestRealParallelIntegration:
    """Tier 2: real hardware, real overlap timing."""

    def test_two_independent_tasks_run_in_parallel(self, ray_local, real_checkouts):
        tasks = (task("a"), task("b"))
        llm = FakeLLM(responses=["edit a", "edit b"])

        started = time.time()
        results = integrate_parallel(real_checkouts, make_spec(), tasks, llm)
        wall = time.time() - started

        assert [r.task.id for r in results] == ["a", "b"]
        assert all(r.passed for r in results)
        serial = sum(r.build.wall_time_s + (r.run.wall_time_s if r.run else 0) for r in results)
        print(f"\nparallel={wall:.0f}s serial-equivalent={serial:.0f}s")
        assert wall < serial * 0.9, "tasks did not overlap"
