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

    def test_the_first_four_tasks_are_the_verified_table_1_cells(self):
        first = load_suite(SUITE)[:4]
        assert [t.id for t in first] == ["ariane-2x2-barrier", "ariane-4x4-barrier", "pico-2x2-addi", "pico-4x4-addi"]
        assert all(t.verified for t in first)

    def test_the_co_design_task_s_grid_fits_its_simulations(self):
        (task,) = [t for t in load_suite(SUITE) if t.kind == "codesign"]
        assert len(task.codesign.grid_designs()) <= task.codesign.simulations
        assert task.workloads == ("matmul.c",)

    def test_every_pico_task_names_the_bist_define(self):
        for task in load_suite(SUITE):
            if task.core == "pico":
                assert "CONFIG_DISABLE_BIST_CLEAR" in task.objective
                assert task.expert_config_rtl == ("CONFIG_DISABLE_BIST_CLEAR",)


class TestTheRtlParamSuite:
    RTLPARAM_SUITE = SUITE.with_name("tasks_rtlparam.yaml")

    def test_loads_one_task_per_mesh_with_the_scaled_program(self):
        tasks = load_suite(self.RTLPARAM_SUITE)
        assert [t.id for t in tasks] == [
            "ariane-2x2-matmul-scaled", "ariane-4x2-matmul-scaled", "ariane-4x4-matmul-scaled",
        ]
        assert [t.mesh for t in tasks] == [(2, 2), (4, 2), (4, 4)]
        assert all(t.kind == "rtlparam" and t.workloads == ("matmul_scaled.c",) for t in tasks)

    def test_no_task_is_a_single_core_chip(self):
        """A 1x1 mesh has no interconnect to contend for, so it is not part of the study."""
        assert all(t.mesh[0] * t.mesh[1] > 1 for t in load_suite(self.RTLPARAM_SUITE))

    def test_every_task_searches_the_same_space_with_the_same_budget(self):
        first, *rest = [t.rtlparam for t in load_suite(self.RTLPARAM_SUITE)]
        assert all(config == first for config in rest)

    def test_the_workload_is_one_of_the_checksummed_gate_programs(self):
        from mace.workloads import compute_checksums

        for task in load_suite(self.RTLPARAM_SUITE):
            assert set(task.workloads) <= set(compute_checksums())

    def test_the_budget_is_the_default_design_s_storage_so_a_larger_buffer_must_be_paid_for(self):
        (task, *_) = load_suite(self.RTLPARAM_SUITE)
        assert task.rtlparam.storage_budget_ratio == 1.0
        budget = task.rtlparam.storage_budget_bits()
        assert budget == 0
        costs = [d.storage_delta() for d in task.rtlparam.space.one_at_a_time()]
        assert any(c <= budget for c in costs) and any(c > budget for c in costs)
        # A larger buffer paired with a smaller one fits.
        space = task.rtlparam.space
        assert space.design({"scoreboard": 4, "l2_mshr": 16}).storage_delta() <= budget


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

    def test_a_task_s_simulation_limits_reach_its_spec(self, tmp_path):
        text = MINIMAL.replace("    verified: true\n", "    verified: true\n    rtl_timeout: 4000000\n    max_cycle: 6000000\n")
        (task,) = load_suite(write(tmp_path, text))
        assert (task.spec().rtl_timeout, task.spec().max_cycle) == (4_000_000, 6_000_000)

    def test_a_task_without_limits_leaves_them_to_the_defaults(self, tmp_path):
        (task,) = load_suite(write(tmp_path, MINIMAL))
        assert (task.spec().rtl_timeout, task.spec().max_cycle) == (None, None)

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
            ("verified: true", "verified: true\n    max_cycle: 0", "max_cycle"),
        ],
    )
    def test_a_malformed_entry_fails_to_load(self, tmp_path, old, new, message):
        with pytest.raises(SuiteError, match=message):
            load_suite(write(tmp_path, MINIMAL.replace(old, new)))

    def test_duplicate_ids_fail(self, tmp_path):
        text = MINIMAL + MINIMAL.split("tasks:\n", 1)[1]
        with pytest.raises(SuiteError, match="duplicate"):
            load_suite(write(tmp_path, text))


CODESIGN = MINIMAL.replace("id: pico-2x2-addi", "id: cd-task").replace("mesh: [2, 2]", "mesh: [4, 1]") + """\
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


RTLPARAM = MINIMAL.replace("id: pico-2x2-addi", "id: rp-task").replace("core: pico", "core: ariane").replace(
    "      config_rtl: [CONFIG_DISABLE_BIST_CLEAR]\n", ""
) + """\
    rtlparam:
      simulations: 6
      storage_budget_ratio: 1.25
      space:
        l2_mshr: [4, 8, 16]
        home_alloc: [LOW, MIXED]
"""


class TestRtlParamBlock:
    def test_loads_the_space_the_simulations_and_the_budget(self, tmp_path):
        (task,) = load_suite(write(tmp_path, RTLPARAM))
        assert task.kind == "rtlparam"
        config = task.rtlparam
        assert (config.simulations, config.storage_budget_ratio) == (6, 1.25)
        assert config.space.knobs == {"home_alloc": ("LOW", "MIXED"), "l2_mshr": (4, 8, 16)}
        assert task.codesign is None

    def test_the_budget_is_the_ratio_s_excess_times_the_default_design_s_storage(self, tmp_path):
        (task,) = load_suite(write(tmp_path, RTLPARAM))
        # The default design holds 8 L2 MSHR entries of 154 bits; the ratio adds a quarter of that.
        assert task.rtlparam.space.default().storage_total() == 8 * 154
        assert task.rtlparam.storage_budget_bits() == 308

    def test_a_ratio_of_one_allows_no_added_storage_and_below_one_requires_savings(self, tmp_path):
        one = load_suite(write(tmp_path, RTLPARAM.replace("1.25", "1.0")))[0].rtlparam
        below = load_suite(write(tmp_path, RTLPARAM.replace("1.25", "0.5")))[0].rtlparam
        assert one.storage_budget_bits() == 0
        assert below.storage_budget_bits() == -616

    def test_the_simulations_default_to_ten_and_the_ratio_to_one(self, tmp_path):
        text = RTLPARAM.replace("      simulations: 6\n", "").replace("      storage_budget_ratio: 1.25\n", "")
        (task,) = load_suite(write(tmp_path, text))
        assert (task.rtlparam.simulations, task.rtlparam.storage_budget_ratio) == (10, 1.0)

    TRADES = RTLPARAM.replace("1.25", "1.0").replace(
        "        home_alloc: [LOW, MIXED]\n",
        "        home_alloc: [LOW, MIXED]\n        scoreboard: [4, 8, 16]\n",
    )

    def test_trades_load_as_designs_of_the_space(self, tmp_path):
        text = self.TRADES + "      trades:\n        - {l2_mshr: 16, scoreboard: 4}\n        - {home_alloc: LOW}\n"
        (task,) = load_suite(write(tmp_path, text))
        trades = task.rtlparam.trades
        assert [t.changed() for t in trades] == [{"l2_mshr": 16, "scoreboard": 4}, {"home_alloc": "LOW"}]
        assert all(task.rtlparam.space.contains(t) for t in trades)

    def test_a_task_without_trades_has_none(self, tmp_path):
        (task,) = load_suite(write(tmp_path, RTLPARAM))
        assert task.rtlparam.trades == ()

    @pytest.mark.parametrize(
        "entry,message",
        [
            ("{l2_mshr: 16}", "over the budget"),
            ("{l2_mshr: 16, scoreboard: 4, store_buffer: 2}", "unknown|maps searched parameters|outside"),
            ("{l2_mshr: 8}", "default design or a repeat"),
            ("{}", "maps searched parameters"),
            ("{l3_mshr: 4}", "maps searched parameters"),
            ("{scoreboard: 32}", "outside the space"),
            ("[l2_mshr, 16]", "maps searched parameters"),
        ],
    )
    def test_a_trade_that_cannot_be_simulated_in_budget_is_refused_at_load(self, tmp_path, entry, message):
        text = self.TRADES + f"      trades:\n        - {entry}\n"
        with pytest.raises(SuiteError, match=message):
            load_suite(write(tmp_path, text))

    def test_a_repeated_trade_is_refused(self, tmp_path):
        text = self.TRADES + "      trades:\n        - {l2_mshr: 16, scoreboard: 4}\n        - {scoreboard: 4, l2_mshr: 16}\n"
        with pytest.raises(SuiteError, match="repeat"):
            load_suite(write(tmp_path, text))

    def test_trades_must_be_a_list(self, tmp_path):
        with pytest.raises(SuiteError, match="list of designs"):
            load_suite(write(tmp_path, self.TRADES + "      trades: {l2_mshr: 16}\n"))

    def test_a_null_ratio_sets_no_budget(self, tmp_path):
        (task,) = load_suite(write(tmp_path, RTLPARAM.replace("1.25", "null")))
        assert task.rtlparam.storage_budget_bits() is None

    def test_a_task_without_the_block_is_not_an_rtl_parameter_task(self, tmp_path):
        (task,) = load_suite(write(tmp_path, MINIMAL))
        assert task.rtlparam is None and task.kind == "bringup"

    @pytest.mark.parametrize(
        "old,new,message",
        [
            ("      simulations: 6\n", "      simulations: 0\n", "simulations"),
            ("      simulations: 6\n", "      simulations: many\n", "simulations"),
            ("      storage_budget_ratio: 1.25\n", "      storage_budget_ratio: 0\n", "storage_budget_ratio"),
            ("      storage_budget_ratio: 1.25\n", "      storage_budget_ratio: -1\n", "storage_budget_ratio"),
            ("      storage_budget_ratio: 1.25\n", "      storage_budget_ratio: big\n", "storage_budget_ratio"),
            ("      storage_budget_ratio: 1.25\n", "      storage_budget_ratio: 1.25\n      colour: red\n", "takes only"),
            ("l2_mshr: [4, 8, 16]", "l3_mshr: [4, 8]", "unknown RTL parameter"),
            ("l2_mshr: [4, 8, 16]", "l2_mshr: [4, 8, 32]", "must come from"),
            ("l2_mshr: [4, 8, 16]", "l2_mshr: [4, 16]", "include the default"),
            ("l2_mshr: [4, 8, 16]", "l2_mshr: 8", "list of values"),
            ("      space:\n        l2_mshr: [4, 8, 16]\n        home_alloc: [LOW, MIXED]\n", "      space: {}\n", "needs a space"),
        ],
    )
    def test_a_malformed_block_fails_to_load(self, tmp_path, old, new, message):
        with pytest.raises(SuiteError, match=message):
            load_suite(write(tmp_path, RTLPARAM.replace(old, new)))

    def test_a_task_cannot_have_both_blocks(self, tmp_path):
        both = RTLPARAM + "    codesign:\n" + CODESIGN.split("    codesign:\n", 1)[1]
        with pytest.raises(SuiteError, match="not both"):
            load_suite(write(tmp_path, both))


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

    def test_a_grid_design_against_the_way_rule_fails_to_load_on_ariane(self, tmp_path):
        text = CODESIGN.replace("assocs: [2, 4]}", "assocs: [2, 4, 8]}").replace(
            "        l1d_size: [4096, 8192]\n", "        l1d_size: [4096, 8192]\n        l1d_assoc: [8]\n"
        )
        load_suite(write(tmp_path, text))  # the rule is Ariane's, so a PicoRV32 task keeps this grid
        with pytest.raises(SuiteError, match="more than the L1.5"):
            load_suite(write(tmp_path, text.replace("core: pico", "core: ariane")))

    def test_the_crossbar_needs_a_mesh_with_one_row(self, tmp_path):
        text = CODESIGN.replace("mesh: [4, 1]", "mesh: [2, 2]")
        with pytest.raises(SuiteError, match="xbar_config needs a mesh with one row"):
            load_suite(write(tmp_path, text))


class TestCodesignGeometries:
    def test_a_geometry_the_area_model_cannot_price_fails_to_load(self, tmp_path):
        text = CODESIGN.replace("l1d: {sizes: [4096, 8192], assocs: [2, 4]}", "l1d: {sizes: [4096, 8192], assocs: [2, 3]}")
        with pytest.raises(SuiteError, match="whole number"):
            load_suite(write(tmp_path, text))


class TestWorkloadArgs:
    def test_a_program_s_finish_mask_and_run_args_reach_the_spec(self, tmp_path):
        text = MINIMAL.replace(
            "    verified: true\n",
            "    verified: true\n    workload_args:\n      addi.S:\n        finish_mask: \"3333\"\n"
            "        run_args: [-midas_args=-DTHREAD_COUNT=8]\n",
        )
        (task,) = load_suite(write(tmp_path, text))
        assert task.spec().run_options("addi.S") == ("3333", ("-midas_args=-DTHREAD_COUNT=8",))

    @pytest.mark.parametrize("mask", ["0011", "0x33", "3333"])
    def test_an_unquoted_finish_mask_is_refused(self, tmp_path, mask):
        text = MINIMAL.replace(
            "    verified: true\n", f"    verified: true\n    workload_args:\n      addi.S:\n        finish_mask: {mask}\n"
        )
        with pytest.raises(SuiteError, match="quoted string"):
            load_suite(write(tmp_path, text))

    def test_an_unknown_option_is_refused(self, tmp_path):
        text = MINIMAL.replace("    verified: true\n", "    verified: true\n    workload_args:\n      addi.S: {threads: 8}\n")
        with pytest.raises(SuiteError, match="finish_mask and run_args"):
            load_suite(write(tmp_path, text))


class TestSourceFault:
    FAULT = (
        "    source_fault:\n      - path: piton/design/chip/tile/pico/rtl/picorv32.v\n"
        "        old: \"assign pcpi_rs1 = reg_op1;\"\n        new: \"assign pcpi_rs1 = reg_op1x;\"\n"
    )

    def test_a_fault_reaches_the_task(self, tmp_path):
        text = MINIMAL.replace("    verified: true\n", "    verified: true\n" + self.FAULT)
        (task,) = load_suite(write(tmp_path, text))
        (edit,) = task.source_fault
        assert (edit.path, edit.new) == ("piton/design/chip/tile/pico/rtl/picorv32.v", "assign pcpi_rs1 = reg_op1x;")

    def test_a_path_outside_the_design_is_refused(self, tmp_path):
        text = MINIMAL.replace("    verified: true\n", "    verified: true\n" + self.FAULT.replace("piton/design/", "piton/verif/"))
        with pytest.raises(SuiteError, match="piton/design/"):
            load_suite(write(tmp_path, text))

    def test_an_entry_with_a_missing_key_is_refused(self, tmp_path):
        text = MINIMAL.replace(
            "    verified: true\n", "    verified: true\n    source_fault:\n      - {path: piton/design/x.v, old: a}\n"
        )
        with pytest.raises(SuiteError, match="path, old, and new"):
            load_suite(write(tmp_path, text))

    def test_the_rtl_suite_loads_with_a_fault_on_every_task(self):
        tasks = load_suite(SUITE.parent / "tasks_rtl.yaml")
        assert [t.id for t in tasks] == [
            "ariane-2x2-hello-rtlfault", "pico-2x2-addi-rtlfault", "ariane-2x2-accu-rtlfault",
        ]
        assert all(t.source_fault for t in tasks)
