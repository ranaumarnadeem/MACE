"""Tier-0 tests for mace.eval.faults.

Run:
    pytest mace/test/test_eval_faults.py -q
"""

from __future__ import annotations

from mace.eval.faults import BIST_DEFINE, FAULTS, FPGA_DEFINE, first_plan_breaker
from mace.spec import Task


def task(kind="config", config_rtl=None):
    return Task(id="t1", deps=(), kind=kind, spec="s", config_rtl=config_rtl)


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

    def test_a_fault_applies_only_to_its_cores(self):
        assert FAULTS["drop_bist"].applies_to("pico", (2, 2))
        assert not FAULTS["drop_bist"].applies_to("ariane", (2, 2))

    def test_unit_test_tasks_are_left_alone(self):
        unit = task(kind="unit_test")
        assert FAULTS["fpga_synth"].break_task(unit) is unit


class TestFirstPlanBreaker:
    def test_breaks_only_the_first_plan(self):
        hook = first_plan_breaker(FAULTS["fpga_synth"])
        plan = (task(), task(kind="unit_test"))
        first = hook(0, plan)
        assert first[0].config_rtl == (FPGA_DEFINE,)
        assert first[1] is plan[1]
        assert hook(1, plan) == plan
