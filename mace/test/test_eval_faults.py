"""Tier-0 tests for mace.eval.faults.

Run:
    pytest mace/test/test_eval_faults.py -q
"""

from __future__ import annotations

from mace.eval.faults import BIST_DEFINE, FAULTS, FPGA_DEFINE, first_design_breaker, first_plan_breaker
from mace.spec import Task


def task(kind="config", config_rtl=None, caches=None):
    return Task(id="t1", deps=(), kind=kind, spec="s", config_rtl=config_rtl, caches=caches)


class TestFaults:
    def test_the_hackathon_faults_are_verified(self):
        assert FAULTS["fpga_synth"].verified and FAULTS["drop_bist"].verified
        assert FAULTS["fpga_synth"].cores == frozenset(("ariane", "pico"))
        assert FAULTS["drop_bist"].cores == frozenset(("pico",))

    def test_fpga_synth_adds_its_define(self):
        broken = FAULTS["fpga_synth"].break_task(task(config_rtl=("CONFIG_DISABLE_BIST_CLEAR",)))
        assert broken.config_rtl == ("CONFIG_DISABLE_BIST_CLEAR", FPGA_DEFINE)

    def test_drop_bist_removes_its_define(self):
        assert FAULTS["drop_bist"].break_task(task(config_rtl=(BIST_DEFINE,))).config_rtl is None

    def test_the_crossbar_fault_selects_the_crossbar(self):
        assert FAULTS["crossbar"].break_task(task()).network == "xbar_config"
        assert FAULTS["crossbar"].verified
        assert FAULTS["crossbar"].cores == frozenset(("ariane", "pico"))

    def test_the_crossbar_fault_breaks_only_a_mesh_with_two_or_more_rows(self):
        crossbar = FAULTS["crossbar"]
        assert crossbar.applies_to("ariane", (2, 2))
        assert not crossbar.applies_to("ariane", (4, 1))

    def test_the_eight_way_l1d_breaks_ariane_s_way_rule(self):
        from chia_openpiton.state_def import PitonConfig

        fault = FAULTS["l1d_eight_way"]
        broken = fault.break_task(task(caches=(("l2", (32768, 4)),)))
        assert broken.caches_dict == {"l1d": (8192, 8), "l2": (32768, 4)}
        assert PitonConfig(core="ariane", caches=broken.caches_dict).way_violations()
        assert fault.verified
        assert fault.applies_to("ariane", (2, 2)) and not fault.applies_to("pico", (2, 2))

    def test_a_fault_applies_only_to_its_cores(self):
        assert FAULTS["drop_bist"].applies_to("pico", (2, 2))
        assert not FAULTS["drop_bist"].applies_to("ariane", (2, 2))

    def test_unit_test_tasks_are_left_alone(self):
        unit = task(kind="unit_test")
        assert FAULTS["fpga_synth"].break_task(unit) is unit


class TestHeldOutFaults:
    HELD_OUT = ("l15_eight_way", "no_mem_ctrl", "l2_direct_mapped", "drop_monitoring")

    def test_exactly_the_four_new_faults_are_held_out(self):
        assert {name for name, fault in FAULTS.items() if fault.held_out} == set(self.HELD_OUT)

    def test_the_faultchecks_verified_each_fault_on_the_cores_it_failed_on(self):
        # faultcheck_<name>, 2026-10-12: the l15 fault passed on pico, drop_monitoring on ariane.
        assert all(FAULTS[name].verified for name in self.HELD_OUT)
        assert FAULTS["l15_eight_way"].cores == frozenset(("ariane",))
        assert FAULTS["drop_monitoring"].cores == frozenset(("pico",))
        assert FAULTS["no_mem_ctrl"].cores == frozenset(("ariane", "pico"))
        assert FAULTS["l2_direct_mapped"].cores == frozenset(("ariane", "pico"))

    def test_the_hackathon_faults_are_not_held_out(self):
        for name in ("fpga_synth", "drop_bist", "crossbar", "l1d_eight_way"):
            assert not FAULTS[name].held_out

    def test_the_eight_way_l15_keeps_ariane_s_way_rule(self):
        from chia_openpiton.state_def import PitonConfig

        broken = FAULTS["l15_eight_way"].break_task(task(caches=(("l1d", (8192, 4)),)))
        assert broken.caches_dict == {"l1d": (8192, 4), "l15": (8192, 8)}
        assert not PitonConfig(core="ariane", caches=broken.caches_dict).way_violations()

    def test_no_mem_ctrl_adds_its_define(self):
        broken = FAULTS["no_mem_ctrl"].break_task(task(config_rtl=(BIST_DEFINE,)))
        assert broken.config_rtl == (BIST_DEFINE, "PITONSYS_NO_MC")

    def test_the_direct_mapped_l2_has_one_way(self):
        assert FAULTS["l2_direct_mapped"].break_task(task()).caches_dict == {"l2": (65536, 1)}

    def test_drop_monitoring_removes_a_default_define_from_the_build(self):
        from mace.loop import _config_for_task
        from mace.spec import MaceSpec

        broken = FAULTS["drop_monitoring"].break_task(task(config_rtl=(BIST_DEFINE,)))
        assert broken.drop_rtl == ("MINIMAL_MONITORING",)
        config = _config_for_task(MaceSpec(workloads=("w.c",), objective="o"), broken)
        assert config.config_rtl == (BIST_DEFINE,)

    def test_drop_monitoring_applies_to_a_task_with_no_other_define(self):
        from mace.loop import _config_for_task
        from mace.spec import MaceSpec

        config = _config_for_task(MaceSpec(workloads=("w.c",), objective="o"), FAULTS["drop_monitoring"].break_task(task()))
        assert config.config_rtl == ()


class TestFirstPlanBreaker:
    def test_breaks_only_the_first_plan(self):
        hook = first_plan_breaker(FAULTS["fpga_synth"])
        plan = (task(), task(kind="unit_test"))
        first = hook(0, plan)
        assert first[0].config_rtl == (FPGA_DEFINE,)
        assert first[1] is plan[1]
        assert hook(1, plan) == plan


class TestFirstDesignBreaker:
    def test_breaks_only_the_first_design(self):
        hook = first_design_breaker(FAULTS["fpga_synth"])
        design = task()
        assert hook(0, design).config_rtl == (FPGA_DEFINE,)
        assert hook(1, design) is design
