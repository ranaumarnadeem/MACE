"""Tier-0 tests for mace.integrator.

Run:
    pytest mace/test/test_integrator.py -q
"""

from __future__ import annotations

import pytest

from mace.integrator import integrate, topological_levels, topological_order
from mace.spec import MaceSpec, Task
from mace.test.conftest import FakeLLM


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
