"""Tier-0 tests for mace.integrator.

Run:
    pytest mace/test/test_integrator.py -q
"""

from __future__ import annotations

import time

import pytest

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace.integrator import (
    _run_batch,
    close_nodes,
    integrate,
    open_nodes,
    topological_levels,
    topological_order,
)
from mace.spec import MaceSpec, Task
from mace.test.conftest import FakeLLM
from mace.workloads import WORKLOADS_DIR


def task(id, deps=(), kind="workload", spec="hello_world.c"):
    return Task(id=id, deps=deps, kind=kind, spec=spec)


def make_spec(**override):
    kwargs = {"workloads": ("hello_world.c",), "objective": "bring up 1x1 ariane"}
    kwargs.update(override)
    return MaceSpec(**kwargs)


class TestTopologicalOrder:
    def test_empty_is_empty(self):
        assert topological_order(()) == ()

    def test_single_task(self):
        t = task("a")
        assert topological_order((t,)) == (t,)

    def test_already_in_order_is_preserved(self):
        a, b, c = task("a"), task("b", deps=("a",)), task("c", deps=("a", "b"))
        assert topological_order((a, b, c)) == (a, b, c)

    def test_out_of_order_input_is_sorted(self):
        a, b = task("a"), task("b", deps=("a",))
        assert topological_order((b, a)) == (a, b)

    def test_diamond_dependency(self):
        a = task("a")
        b = task("b", deps=("a",))
        c = task("c", deps=("a",))
        d = task("d", deps=("b", "c"))
        order = topological_order((d, c, b, a))
        assert order.index(a) < order.index(b) < order.index(d)
        assert order.index(a) < order.index(c) < order.index(d)

    def test_independent_roots_keep_relative_input_order(self):
        a, b = task("a"), task("b")
        assert topological_order((a, b)) == (a, b)
        assert topological_order((b, a)) == (b, a)

    def test_unknown_dep_raises(self):
        with pytest.raises(ValueError, match="unknown task"):
            topological_order((task("a", deps=("nope",)),))

    def test_cycle_raises(self):
        a = task("a", deps=("b",))
        b = task("b", deps=("a",))
        with pytest.raises(ValueError, match="cycle"):
            topological_order((a, b))

    def test_duplicate_id_raises(self):
        with pytest.raises(ValueError, match="duplicate task ids"):
            topological_order((task("a"), task("a")))


class TestTopologicalLevels:
    def test_empty_is_empty(self):
        assert topological_levels(()) == ()

    def test_independent_roots_are_one_level(self):
        a, b = task("a"), task("b")
        assert topological_levels((a, b)) == ((a, b),)

    def test_linear_chain_is_one_task_per_level(self):
        a, b = task("a"), task("b", deps=("a",))
        assert topological_levels((a, b)) == ((a,), (b,))

    def test_diamond_dependency(self):
        a = task("a")
        b = task("b", deps=("a",))
        c = task("c", deps=("a",))
        d = task("d", deps=("b", "c"))
        assert topological_levels((a, b, c, d)) == ((a,), (b, c), (d,))

    def test_unknown_dep_raises(self):
        with pytest.raises(ValueError, match="unknown task"):
            topological_levels((task("a", deps=("nope",)),))

    def test_cycle_raises(self):
        a = task("a", deps=("b",))
        b = task("b", deps=("a",))
        with pytest.raises(ValueError, match="cycle"):
            topological_levels((a, b))

    def test_duplicate_id_raises(self):
        with pytest.raises(ValueError, match="duplicate task ids"):
            topological_levels((task("a"), task("a", deps=("b",)), task("b")))

    def test_flattening_levels_matches_topological_order(self):
        a = task("a")
        b = task("b", deps=("a",))
        c = task("c", deps=("a",))
        flattened = tuple(t for level in topological_levels((c, b, a)) for t in level)
        assert flattened == topological_order((c, b, a))


class TestIntegrate:
    def test_all_tasks_pass_in_dependency_order(self, stub_piton_root, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        tasks = (task("cfg1"), task("run_hello", deps=("cfg1",)))
        llm = FakeLLM(responses=["edit cfg", "edit hello"])

        results = integrate(str(stub_piton_root), make_spec(), tasks, llm)

        assert [r.task.id for r in results] == ["cfg1", "run_hello"]
        assert all(r.passed for r in results)

    def test_stops_at_first_failure(self, stub_piton_root, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "fail")
        tasks = (task("cfg1"), task("run_hello", deps=("cfg1",)))
        llm = FakeLLM(responses=["edit cfg", "edit hello"])

        results = integrate(str(stub_piton_root), make_spec(), tasks, llm)

        assert [r.task.id for r in results] == ["cfg1"]
        assert results[0].passed is False

    def test_independent_task_after_a_failure_is_not_attempted(self, stub_piton_root, monkeypatch):
        """Serial + fail-fast: even a task with no dependency on the failed
        one does not run once something earlier in the order has failed --
        this integrator has no fan-out yet (see the plan's step 7)."""
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "fail")
        tasks = (task("a"), task("b"))  # both roots, independent of each other
        llm = FakeLLM(responses=["edit a", "edit b"])

        results = integrate(str(stub_piton_root), make_spec(), tasks, llm)

        assert [r.task.id for r in results] == ["a"]

    def test_uses_the_one_provided_checkout(self, stub_piton_root, monkeypatch, sims_argv):
        """integrate() takes a single piton_root, not one per task -- every
        task's build+run must land in that same checkout's stub sims log."""
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        tasks = (task("a"), task("b", deps=("a",)))
        llm = FakeLLM(responses=["edit a", "edit b"])

        integrate(str(stub_piton_root), make_spec(), tasks, llm)

        # a's build (real) + a's run + b's build (same spec -> same config ->
        # served from the build-reuse marker a's build just wrote, no sims
        # call) + b's run: 3 real sims invocations, all in one checkout's log.
        assert len(sims_argv.lines()) == 3

    def test_empty_task_list_produces_no_results(self, stub_piton_root):
        assert integrate(str(stub_piton_root), make_spec(), (), FakeLLM(responses=[])) == ()


class TestOpenNodes:
    def test_empty_piton_roots_returns_empty_list(self):
        assert open_nodes(()) == []

    def test_preserves_the_order_of_piton_roots(self, monkeypatch):
        import mace.integrator as integrator_module

        class _FakeNode:
            def __init__(self, root, pg_ready_timeout_s=120):
                self.root = root

        monkeypatch.setattr(integrator_module, "OpenPitonWorkspaceNode", _FakeNode)

        nodes = open_nodes(("/a", "/b", "/c"))

        assert [n.root for n in nodes] == ["/a", "/b", "/c"]

    def test_constructs_concurrently_not_sequentially(self, monkeypatch):
        import mace.integrator as integrator_module

        class _SlowFakeNode:
            def __init__(self, root, pg_ready_timeout_s=120):
                time.sleep(0.2)
                self.root = root

        monkeypatch.setattr(integrator_module, "OpenPitonWorkspaceNode", _SlowFakeNode)

        started = time.monotonic()
        open_nodes(("/a", "/b", "/c"))
        elapsed = time.monotonic() - started

        # Sequential would take ~0.6s; concurrent should be close to ~0.2s.
        assert elapsed < 0.45

    def test_order_preserved_even_when_construction_finishes_out_of_order(self, monkeypatch):
        import mace.integrator as integrator_module

        class _VariableDelayNode:
            _delays = {"/slow": 0.15, "/fast": 0.0}

            def __init__(self, root, pg_ready_timeout_s=120):
                time.sleep(self._delays[root])
                self.root = root

        monkeypatch.setattr(integrator_module, "OpenPitonWorkspaceNode", _VariableDelayNode)

        nodes = open_nodes(("/slow", "/fast"))

        assert [n.root for n in nodes] == ["/slow", "/fast"]

    def test_partial_failure_closes_the_nodes_that_did_succeed(self, monkeypatch):
        """If one checkout's construction fails, the other's already-live
        node must not be discarded with nothing left to close it -- that
        would permanently leak its placement group."""
        import mace.integrator as integrator_module

        closed = []

        class _Node:
            def __init__(self, root, pg_ready_timeout_s=120):
                if root == "/bad":
                    raise TimeoutError("placement group never became ready")
                self.root = root

            def close(self):
                closed.append(self.root)

        monkeypatch.setattr(integrator_module, "OpenPitonWorkspaceNode", _Node)

        with pytest.raises(TimeoutError):
            open_nodes(("/good", "/bad"))

        assert closed == ["/good"]


class TestCloseNodes:
    def test_one_nodes_close_failure_does_not_block_closing_the_rest(self):
        closed = []

        class _Node:
            def __init__(self, name, should_raise=False):
                self.name = name
                self.should_raise = should_raise

            def close(self):
                if self.should_raise:
                    raise RuntimeError("real Ray GCS RPC failure")
                closed.append(self.name)

        nodes = [_Node("a"), _Node("b", should_raise=True), _Node("c")]

        close_nodes(nodes)  # must not raise -- and must still close a and c

        assert closed == ["a", "c"]


class _NodeStub:
    """Just enough of OpenPitonWorkspaceNode for _run_batch's local
    unit_test path: a real ``piton_root`` attribute, no remote methods --
    deliberately, so a test can prove a unit_test-only batch never touches
    them (the whole point of the fix under test).
    """

    def __init__(self, piton_root):
        self.piton_root = str(piton_root)


class TestRunBatchUnitTestDispatch:
    """A unit_test-kind task in integrate_parallel's batch must go through
    mace.loop.run_mace_step (scaffold, build, never run) against its own
    checkout, exactly like integrate()'s serial path -- not the remote
    manycore prompt/build/run pipeline, which would treat the task's spec
    (a bare RTL path) as an edit instruction and build the full mesh
    instead of the scaffolded single-module env. See _run_batch's own
    docstring for the bug this guards.
    """

    def _rtl_module(self, stub_piton_root, rel_path="design/foo.v"):
        rtl = stub_piton_root / rel_path
        rtl.parent.mkdir(parents=True, exist_ok=True)
        rtl.write_text("module foo (\n  input clk,\n  output reg done\n);\nendmodule\n")
        return rel_path

    def test_runs_locally_never_touching_the_remote_pipeline(self, stub_piton_root, monkeypatch):
        env_dir = stub_piton_root / "piton" / "verif" / "env" / "design_foo_ut"
        env_dir.mkdir(parents=True)
        rel_path = self._rtl_module(stub_piton_root)
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["reconciled the ports"])
        unit_task = task("t1", kind="unit_test", spec=rel_path)
        node = _NodeStub(stub_piton_root)  # no .prompt/.build/.run -- must stay untouched

        results = _run_batch(
            [node], make_spec(), [unit_task], llm, (), str(WORKLOADS_DIR), None, 0
        )

        assert len(results) == 1
        assert results[0].task is unit_task
        assert results[0].build.success is True
        assert results[0].run is None  # unit_test is gated on build only, never run
        assert results[0].passed is True

    def test_on_task_progress_fires_before_the_local_build(self, stub_piton_root, monkeypatch):
        """Real-time in-flight feedback: a caller must be told this task is
        building BEFORE the (potentially slow) call, not only see it after
        the fact via the finished result.
        """
        env_dir = stub_piton_root / "piton" / "verif" / "env" / "design_foo_ut"
        env_dir.mkdir(parents=True)
        rel_path = self._rtl_module(stub_piton_root)
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        llm = FakeLLM(responses=["reconciled the ports"])
        unit_task = task("t1", kind="unit_test", spec=rel_path)
        node = _NodeStub(stub_piton_root)
        events = []

        _run_batch(
            [node], make_spec(), [unit_task], llm, (), str(WORKLOADS_DIR), None, 0,
            on_task_progress=lambda task_ids, stage: events.append((task_ids, stage)),
        )

        assert events == [(("t1",), "building")]


class _FakeRef:
    """Stand-in for a real chia_remote() dispatch: resolves to *value* after
    *delay* seconds, once mace.integrator.get() is called on it."""

    def __init__(self, value, delay=0.0):
        self.value = value
        self.delay = delay


def _fake_get(ref):
    time.sleep(ref.delay)
    return ref.value


class _FakePromptAttr:
    """llm.prompt.chia_remote -- one shared llm dispatches every task's
    prompt, so the delay is looked up per task.spec, not fixed per llm."""

    def __init__(self, delays_by_spec):
        self.delays_by_spec = delays_by_spec

    def chia_remote(self, llm, spec, tools, _chia_tag=None):
        query = FakeLLM(responses=["edit"]).prompt("edit")
        return _FakeRef(query, self.delays_by_spec.get(spec, 0.0))


class _FakeBuildAttr:
    def __init__(self, delay=0.0):
        self.delay = delay

    def chia_remote(self, config, _chia_tag=None):
        build = PitonBuildArtifact(
            success=True, returncode=0, config=config, sim_type="vlt", model_dir="/x",
            binary_path="/x/Vcmp_top", wall_time_s=0.0,
        )
        return _FakeRef(build, self.delay)


class _FakeRunAttr:
    def chia_remote(self, config, workload, asm_diag_root=None, rtl_timeout=None, _chia_tag=None):
        run = PitonRunResult(
            success=True, returncode=0, test=workload, sim_type="vlt", run_dir="/x/runs/1",
            verdict="pass",
        )
        return _FakeRef(run, 0.0)


class _FakeRemoteNode:
    def __init__(self, piton_root, build_delay=0.0):
        self.piton_root = piton_root
        self.build = _FakeBuildAttr(build_delay)
        self.run = _FakeRunAttr()


class TestRunBatchRemotePipelining:
    """Each remote task's own prompt -> build -> run must be pipelined
    independently of its batch-mates -- see _run_batch's own docstring for
    the bug (batch-wide stage synchronization) this guards against.
    """

    def test_a_slow_prompt_on_one_task_does_not_delay_a_fast_task_s_build(self, monkeypatch):
        """task_a: slow prompt, fast build. task_b: fast prompt, slow build.
        Pipelined, the batch takes close to one delay (both run
        concurrently on their own threads); batch-wide stage
        synchronization would take close to the SUM of both delays (every
        prompt collected before any build is even dispatched).
        """
        monkeypatch.setattr("mace.integrator.get", _fake_get)
        delay = 0.25

        llm = type("FakeLLM", (), {"prompt": _FakePromptAttr({"task_a_spec": delay, "task_b_spec": 0.0})})()
        node_a = _FakeRemoteNode("/root_a", build_delay=0.0)
        node_b = _FakeRemoteNode("/root_b", build_delay=delay)
        task_a = Task(id="a", deps=(), kind="workload", spec="task_a_spec")
        task_b = Task(id="b", deps=(), kind="workload", spec="task_b_spec")

        started = time.monotonic()
        results = _run_batch(
            [node_a, node_b], make_spec(), [task_a, task_b], llm, (), str(WORKLOADS_DIR), None, 0
        )
        elapsed = time.monotonic() - started

        assert [r.passed for r in results] == [True, True]
        # Sequential (batch-synchronized) would take ~2*delay; pipelined
        # should be close to one delay, both tasks' pipelines overlapping.
        assert elapsed < delay * 1.75

    def test_a_failed_task_prompt_still_builds_and_runs_the_task(self, monkeypatch):
        """The reply is unused for the build, so an LLM error there, such as
        a reply cut off at the output limit, must not end the run."""

        class _FailingPromptAttr:
            def chia_remote(self, llm, spec, tools, _chia_tag=None):
                return _FakeRef(None)

        def get_or_raise(ref):
            if ref.value is None:
                raise RuntimeError("response truncated at max_output_tokens")
            return ref.value

        monkeypatch.setattr("mace.integrator.get", get_or_raise)
        llm = type("FakeLLM", (), {"prompt": _FailingPromptAttr()})()
        task = Task(id="a", deps=(), kind="workload", spec="spec_a")

        [result] = _run_batch([_FakeRemoteNode("/root_a")], make_spec(), [task], llm, (), str(WORKLOADS_DIR), None, 0)

        assert result.passed is True
        assert result.query.success is False
        assert "max_output_tokens" in result.query.stderr

    def test_progress_callback_reports_each_task_independently(self, monkeypatch):
        monkeypatch.setattr("mace.integrator.get", _fake_get)

        llm = type("FakeLLM", (), {"prompt": _FakePromptAttr({})})()
        node_a = _FakeRemoteNode("/root_a")
        node_b = _FakeRemoteNode("/root_b")
        task_a = Task(id="a", deps=(), kind="workload", spec="spec_a")
        task_b = Task(id="b", deps=(), kind="workload", spec="spec_b")
        events = []
        lock_free_append = lambda task_ids, stage: events.append((task_ids, stage))  # noqa: E731

        _run_batch(
            [node_a, node_b], make_spec(), [task_a, task_b], llm, (), str(WORKLOADS_DIR), None, 0,
            on_task_progress=lock_free_append,
        )

        # Each event names exactly one task -- no batch-wide grouping.
        for task_ids, stage in events:
            assert len(task_ids) == 1
        seen_ids = {task_ids[0] for task_ids, _ in events}
        assert seen_ids == {"a", "b"}
        seen_stages = {stage for _, stage in events}
        assert seen_stages == {"prompting", "building", "running"}
