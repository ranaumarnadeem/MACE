"""Tier-0 tests for mace.eval.suite.

Run:
    pytest mace/test/test_eval_suite.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mace.eval.suite import SuiteError, load_suite

SUITE = Path(__file__).resolve().parents[2] / "examples" / "eval" / "tasks.yaml"


def write(tmp_path, text):
    path = tmp_path / "suite.yaml"
    path.write_text(text)
    return path


MINIMAL = """\
version: 1
defaults:
  budget: {max_iterations: 3, max_wall_s: 3600}
tasks:
  - id: pico-2x2-addi
    core: pico
    mesh: [2, 2]
    workloads: [addi.S]
    objective: Verify addi.S passes.
    verified: true
    expert:
      config_rtl: [CONFIG_DISABLE_BIST_CLEAR]
      caches: {l2: [131072, 4]}
"""


class TestTheRepositorySuite:
    def test_loads(self):
        tasks = load_suite(SUITE)
        assert len({t.id for t in tasks}) == len(tasks)

    def test_the_verified_bring_up_tasks_are_the_table_1_cells(self):
        verified = {t.id for t in load_suite(SUITE) if t.verified and t.kind == "bringup"}
        assert verified == {"ariane-2x2-barrier", "ariane-4x4-barrier", "pico-2x2-addi", "pico-4x4-addi"}

    def test_the_co_design_task_s_grid_fits_its_simulations(self):
        (task,) = [t for t in load_suite(SUITE) if t.kind == "codesign"]
        assert len(task.codesign.grid_designs()) <= task.codesign.simulations
        assert task.workloads == ("matmul.c",)

    def test_every_pico_task_names_the_bist_define(self):
        for task in load_suite(SUITE):
            if task.core == "pico":
                assert "CONFIG_DISABLE_BIST_CLEAR" in task.objective
                assert task.expert_config_rtl == ("CONFIG_DISABLE_BIST_CLEAR",)


class TestLoading:
    def test_a_task_becomes_a_spec_and_an_expert_task(self, tmp_path):
        (task,) = load_suite(write(tmp_path, MINIMAL))
        spec = task.spec()
        assert (spec.core, spec.target_mesh, spec.workloads) == ("pico", (2, 2), ("addi.S",))
        assert spec.budget.max_iterations == 3
        expert = task.expert_task()
        assert expert.config_rtl == ("CONFIG_DISABLE_BIST_CLEAR",)
        assert expert.caches_dict == {"l2": (131072, 4)}

    def test_a_task_budget_overrides_the_defaults_field_by_field(self, tmp_path):
        text = MINIMAL.replace("    verified: true\n", "    verified: true\n    budget: {max_wall_s: 7200}\n")
        (task,) = load_suite(write(tmp_path, text))
        assert (task.budget.max_iterations, task.budget.max_wall_s) == (3, 7200)

    @pytest.mark.parametrize(
        "old,new,message",
        [
            ("version: 1", "version: 2", "version"),
            ("id: pico-2x2-addi", "id: Pico 2x2", "task id"),
            ("mesh: [2, 2]", "mesh: [2]", "mesh"),
            ("workloads: [addi.S]", "workloads: []", "workloads"),
            ("core: pico", "core: rocket", "core"),
            ("l2: [131072, 4]", "l3: [131072, 4]", "expert cache"),
            ("l2: [131072, 4]", "l2: [0, 4]", "pico-2x2-addi"),
            ("CONFIG_DISABLE_BIST_CLEAR", "bad-define", "config_rtl"),
            ("verified: true", "verified: true\n    colour: red", "unknown keys"),
            ("max_iterations: 3", "max_iterations: 0", "defaults"),
        ],
    )
    def test_a_malformed_entry_fails_to_load(self, tmp_path, old, new, message):
        with pytest.raises(SuiteError, match=message):
            load_suite(write(tmp_path, MINIMAL.replace(old, new)))

    def test_duplicate_ids_fail(self, tmp_path):
        text = MINIMAL + MINIMAL.split("tasks:\n", 1)[1]
        with pytest.raises(SuiteError, match="duplicate"):
            load_suite(write(tmp_path, text))


CODESIGN = MINIMAL.replace("id: pico-2x2-addi", "id: cd-task") + """\
    codesign:
      simulations: 20
      batch: 2
      area_budget_ratio: 1.0
      space:
        l1d: {sizes: [4096, 8192], assocs: [2, 4]}
        networks: [2dmesh_config, xbar_config]
      grid:
        l1d_size: [4096, 8192]
        network: [2dmesh_config, xbar_config]
"""


class TestCodesignBlock:
    def test_loads_the_space_and_the_grid(self, tmp_path):
        (task,) = load_suite(write(tmp_path, CODESIGN))
        assert task.kind == "codesign"
        config = task.codesign
        assert (config.simulations, config.batch, config.area_budget_ratio) == (20, 2, 1.0)
        assert config.space.count() == 2 * 2 * 2
        assert len(config.grid_designs()) == 4

    def test_a_task_without_the_block_is_bring_up(self, tmp_path):
        (task,) = load_suite(write(tmp_path, MINIMAL))
        assert (task.kind, task.codesign) == ("bringup", None)

    @pytest.mark.parametrize(
        "old,new,message",
        [
            ("l1d_size: [4096, 8192]", "l1i_size: [4096, 8192]", "outside the space"),
            ("l1d_size: [4096, 8192]", "l1d_size: [2048, 4096]", "outside its space"),
            ("simulations: 20", "simulations: 3", "more than its 3 simulations"),
            ("batch: 2", "batch: 0", "batch"),
            ("area_budget_ratio: 1.0", "area_budget_ratio: -1", "area_budget_ratio"),
            ("l1d: {sizes: [4096, 8192], assocs: [2, 4]}", "l1d: {sizes: [4096, 8192]}", "sizes and assocs"),
            ("networks: [2dmesh_config, xbar_config]", "networks: [torus]", "networks"),
        ],
    )
    def test_a_malformed_block_fails_to_load(self, tmp_path, old, new, message):
        with pytest.raises(SuiteError, match=message):
            load_suite(write(tmp_path, CODESIGN.replace(old, new)))

    def test_a_missing_grid_fails(self, tmp_path):
        text = CODESIGN.split("      grid:")[0]
        with pytest.raises(SuiteError, match="needs a grid"):
            load_suite(write(tmp_path, text))
