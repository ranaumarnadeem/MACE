"""Tier-0 tests for mace.spec.

Run:
    pytest mace/test/test_spec.py -q
"""

from __future__ import annotations

import dataclasses

import pytest

from chia_openpiton.state_def import MAX_TILES_PER_AXIS
from mace.spec import Budget, MaceSpec, Task


def make_spec(**override):
    kwargs = {"workloads": ("hello_world.c",), "objective": "bring up a 2x2 mesh"}
    kwargs.update(override)
    return MaceSpec(**kwargs)


class TestBudgetValidation:
    def test_defaults_are_valid(self):
        b = Budget()
        assert (b.max_iterations, b.max_usd, b.max_wall_s) == (10, 20.0, 3600)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_max_iterations_must_be_positive(self, bad):
        with pytest.raises(ValueError, match="max_iterations"):
            Budget(max_iterations=bad)

    def test_bool_is_not_accepted_as_max_iterations(self):
        with pytest.raises(ValueError, match="max_iterations"):
            Budget(max_iterations=True)

    @pytest.mark.parametrize("bad", [0, -5.0])
    def test_max_usd_must_be_positive(self, bad):
        with pytest.raises(ValueError, match="max_usd"):
            Budget(max_usd=bad)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_max_wall_s_must_be_positive(self, bad):
        with pytest.raises(ValueError, match="max_wall_s"):
            Budget(max_wall_s=bad)


class TestMaceSpecValidation:
    def test_minimal_spec_is_valid(self):
        spec = make_spec()
        assert spec.core == "ariane"
        assert spec.target_mesh == (1, 1)
        assert spec.budget == Budget()

    def test_workloads_must_be_non_empty(self):
        with pytest.raises(ValueError, match="workloads"):
            make_spec(workloads=())

    @pytest.mark.parametrize("bad", ["", "   ", 123])
    def test_workload_names_must_be_non_empty_strings(self, bad):
        with pytest.raises(ValueError, match="workload"):
            make_spec(workloads=(bad,))

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_objective_must_be_non_empty(self, bad):
        with pytest.raises(ValueError, match="objective"):
            make_spec(objective=bad)

    def test_unknown_core_rejected(self):
        with pytest.raises(ValueError, match="core"):
            make_spec(core="pico")

    @pytest.mark.parametrize("bad", [(1,), (1, 1, 1), [1, 1]])
    def test_target_mesh_must_be_a_2tuple(self, bad):
        with pytest.raises(ValueError, match="target_mesh"):
            make_spec(target_mesh=bad)

    @pytest.mark.parametrize("bad", [0, -1, MAX_TILES_PER_AXIS + 1])
    def test_target_mesh_axis_bounds(self, bad):
        with pytest.raises(ValueError, match="target_mesh"):
            make_spec(target_mesh=(bad, 1))

    def test_bool_is_not_accepted_as_a_mesh_axis(self):
        with pytest.raises(ValueError, match="target_mesh"):
            make_spec(target_mesh=(True, 1))

    def test_budget_must_be_a_budget_instance(self):
        with pytest.raises(ValueError, match="budget"):
            make_spec(budget=(10, 20.0, 3600))

    def test_custom_budget_is_kept(self):
        spec = make_spec(budget=Budget(max_iterations=3))
        assert spec.budget.max_iterations == 3


def test_spec_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_spec().objective = "something else"


def test_budget_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Budget().max_iterations = 1


class TestTaskValidation:
    def test_minimal_task_is_valid(self):
        t = Task(id="t1", deps=(), kind="config", spec="x_tiles=2,y_tiles=2")
        assert t.deps == ()

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_id_must_be_non_empty(self, bad):
        with pytest.raises(ValueError, match="task id"):
            Task(id=bad, deps=(), kind="config", spec="s")

    def test_kind_must_be_known(self):
        with pytest.raises(ValueError, match="kind"):
            Task(id="t1", deps=(), kind="rebuild", spec="s")

    @pytest.mark.parametrize("bad_deps", [("",), ("t1", ""), (123,)])
    def test_dep_ids_must_be_non_empty_strings(self, bad_deps):
        with pytest.raises(ValueError, match="dep ids"):
            Task(id="t1", deps=bad_deps, kind="workload", spec="s")

    def test_multiple_deps_kept_in_order(self):
        t = Task(id="t3", deps=("t1", "t2"), kind="workload", spec="hello_world.c")
        assert t.deps == ("t1", "t2")


def test_task_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Task(id="t1", deps=(), kind="config", spec="s").spec = "other"
