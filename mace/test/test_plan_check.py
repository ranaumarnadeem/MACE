"""Tier-0 tests for mace.plan_check.

Run:
    pytest mace/test/test_plan_check.py -q
"""

from __future__ import annotations

import pytest

from mace import plan_check
from mace.plan_check import (
    MISSING_MODULE,
    REPEATED_FAILURE,
    REQUIRED_DEFINE,
    SOURCE_EDIT,
    WAY_RULE,
    check_plan,
    config_key,
    define_index,
    format_rejection,
    names_source_edit,
    objective_defines,
    strip_unknown_defines,
)
from mace.spec import Budget, MaceSpec, Task

BIST = "CONFIG_DISABLE_BIST_CLEAR"
PICO_OBJECTIVE = (
    "Verify the addi.S gate workload passes on a 2x2 pico mesh in a single task. "
    f"That task needs the {BIST} RTL define."
)


def spec(core="pico", objective=PICO_OBJECTIVE, workloads=("addi.S",)):
    return MaceSpec(workloads=workloads, objective=objective, core=core, target_mesh=(2, 2), budget=Budget(max_iterations=3))


def task(id="t1", kind="config", text="Configure a 2x2 mesh", deps=(), config_rtl=None, caches=None):
    return Task(id=id, deps=deps, kind=kind, spec=text, config_rtl=config_rtl, caches=caches)


def rules(problems):
    return [(p.rule, p.task_id) for p in problems]


class TestObjectiveDefines:
    def test_a_sentence_that_mentions_a_define_names_it(self):
        assert objective_defines(PICO_OBJECTIVE) == (BIST,)

    def test_an_objective_with_no_define_names_none(self):
        assert objective_defines("Verify the barrier_atomic.c gate workload passes on a 2x2 mesh.") == ()

    def test_an_upper_snake_name_outside_a_define_sentence_is_not_a_define(self):
        assert objective_defines("Run under VERILATOR_ROOT with DTHREAD_COUNT set. Check hello.") == ()

    def test_every_name_in_the_define_sentence_counts_once(self):
        assert objective_defines("The build needs the A_ONE and B_TWO defines, and A_ONE again.") == ("A_ONE", "B_TWO")


class TestNamesSourceEdit:
    @pytest.mark.parametrize("text", [
        "Update manycore_top.v to correctly define async_mux when PITON_FPGA_SYNTH is enabled.",
        "Fix `async_mux` definition in `manycore_top.v` when `PITON_FPGA_SYNTH` is enabled.",
        "Remove the 'async_mux' port connection in 'manycore_top.v' (line 361)",
        "Conditionally remove async_mux port connection in manycore_top.v (line 361) using ifdef",
    ])
    def test_an_edit_instruction_names_its_file(self, text):
        assert names_source_edit(text) is not None

    def test_the_file_is_named(self):
        assert names_source_edit("Update manycore_top.v now") == "manycore_top.v"

    @pytest.mark.parametrize("text", [
        "Configure 2x2 OpenPiton/Ariane mesh",
        "Run barrier_atomic.c on 2x2 mesh",
        "Verify addi.S passes on the design in picorv32.v",  # names a file, asks for no edit
        "Rebuild with an 8-way L1D and run it again",
        "Update the caches to 4 ways in version 2.v1",  # not a source file name
    ])
    def test_a_config_instruction_names_no_edit(self, text):
        assert names_source_edit(text) is None


class TestSourceEdits:
    def test_an_rtl_task_is_rejected_when_the_run_has_no_rtl_edits(self):
        plan = (task("fix", kind="rtl", text="Fix the adapter"),)
        assert rules(check_plan(spec("ariane", "Verify hello."), plan)) == [(SOURCE_EDIT, "fix")]

    def test_an_rtl_task_is_fine_when_the_run_allows_rtl_edits(self):
        plan = (task("fix", kind="rtl", text="Fix the adapter"),)
        assert check_plan(spec("ariane", "Verify hello."), plan, rtl_edits=True) == ()

    def test_a_config_task_that_asks_for_an_edit_is_rejected(self):
        plan = (task("c1", text="Update manycore_top.v to correctly define async_mux"),)
        problems = check_plan(spec("ariane", "Verify hello."), plan)
        assert rules(problems) == [(SOURCE_EDIT, "c1")]
        assert "manycore_top.v" in problems[0].reason and "CONFIG_RTL:" in problems[0].reason

    def test_a_workload_task_that_asks_for_an_edit_is_rejected(self):
        plan = (task("w1", kind="workload", text="Remove the async_mux port connection in manycore_top.v"),)
        assert rules(check_plan(spec("ariane", "Verify hello."), plan)) == [(SOURCE_EDIT, "w1")]


class TestRequiredDefines:
    def test_the_recorded_pico_plan_with_a_define_free_first_level_is_rejected(self):
        # Run 5cff7a639a2e: cfg_2x2_pico had no define, and cfg_bist_clear, which had it, depended on it.
        plan = (
            task("cfg_2x2_pico", text="Configure a 2x2 pico mesh."),
            task("cfg_bist_clear", deps=("cfg_2x2_pico",), config_rtl=(BIST,)),
            task("workload_addi_s", kind="workload", deps=("cfg_bist_clear",), config_rtl=(BIST,)),
        )
        problems = check_plan(spec(), plan)
        assert rules(problems) == [(REQUIRED_DEFINE, "cfg_2x2_pico")]
        assert f"CONFIG_RTL: cfg_2x2_pico | {BIST}" in problems[0].reason

    def test_a_plan_whose_every_build_task_has_the_define_passes(self):
        plan = (task("a", config_rtl=(BIST,)), task("b", kind="workload", deps=("a",), config_rtl=(BIST,)))
        assert check_plan(spec(), plan) == ()

    def test_an_objective_without_a_define_requires_none(self):
        assert check_plan(spec("ariane", "Verify hello on a 2x2 mesh."), (task("a"),)) == ()

    def test_a_unit_test_task_needs_no_define(self):
        plan = (task("u", kind="unit_test", text="piton/design/x.v"), task("a", config_rtl=(BIST,)))
        assert check_plan(spec(), plan) == ()


class TestStripUnknownDefines:
    KNOWN = frozenset((BIST, "PITON_FPGA_SYNTH"))

    def test_an_invented_define_is_removed_and_a_real_one_stays(self):
        # Run 81fe93533a7a: the planner invented CONFIG_HANDLE_ASYNC_MUX_FOR_FPGA beside the real define.
        plan = (task("c1", config_rtl=("CONFIG_HANDLE_ASYNC_MUX_FOR_FPGA", "PITON_FPGA_SYNTH")),)
        stripped, removed = strip_unknown_defines(plan, self.KNOWN)
        assert stripped[0].config_rtl == ("PITON_FPGA_SYNTH",)
        assert removed == ("CONFIG_HANDLE_ASYNC_MUX_FOR_FPGA",)

    def test_a_task_left_with_no_define_has_none(self):
        stripped, _ = strip_unknown_defines((task("c1", config_rtl=("MADE_UP",)),), self.KNOWN)
        assert stripped[0].config_rtl is None

    def test_every_task_that_carries_the_define_loses_it_and_it_is_named_once(self):
        flags = ("MADE_UP", BIST)
        plan = (task("a", config_rtl=flags), task("b", kind="workload", deps=("a",), config_rtl=flags))
        stripped, removed = strip_unknown_defines(plan, self.KNOWN)
        assert [t.config_rtl for t in stripped] == [(BIST,), (BIST,)]
        assert removed == ("MADE_UP",)

    def test_a_define_in_keep_stays_even_when_no_file_mentions_it(self):
        plan = (task("c1", config_rtl=(BIST,)),)
        stripped, removed = strip_unknown_defines(plan, frozenset(), keep=(BIST,))
        assert stripped == plan and removed == ()

    def test_a_plan_with_only_known_defines_is_returned_as_it_was(self):
        plan = (task("c1", config_rtl=(BIST,)), task("c2"))
        stripped, removed = strip_unknown_defines(plan, self.KNOWN)
        assert stripped[0] is plan[0] and stripped[1] is plan[1] and removed == ()

    def test_without_an_index_nothing_is_stripped(self):
        plan = (task("c1", config_rtl=("MADE_UP",)),)
        assert strip_unknown_defines(plan, None) == (plan, ())

    def test_stripping_first_makes_two_tasks_build_the_same_model(self):
        s = spec("ariane", "Verify hello.")
        plan = (task("a", config_rtl=("MADE_UP",)), task("b"))
        stripped, _ = strip_unknown_defines(plan, self.KNOWN)
        assert config_key(s, stripped[0]) == config_key(s, stripped[1])


class TestWayRule:
    EIGHT_WAY_L1D = (("l1d", (8192, 8)),)

    def test_an_ariane_l1d_with_more_ways_than_the_l15_is_rejected(self):
        plan = (task("c1", caches=self.EIGHT_WAY_L1D),)
        problems = check_plan(spec("ariane", "Verify hello."), plan)
        assert rules(problems) == [(WAY_RULE, "c1")]
        assert "8 ways" in problems[0].reason

    def test_raising_the_l15_too_satisfies_the_rule(self):
        plan = (task("c1", caches=(("l1d", (8192, 8)), ("l15", (8192, 8)))),)
        assert check_plan(spec("ariane", "Verify hello."), plan) == ()

    def test_the_rule_binds_ariane_only(self):
        assert check_plan(spec("pico", "Verify hello."), (task("c1", caches=self.EIGHT_WAY_L1D),)) == ()


class TestMissingModule:
    def test_a_unit_test_of_a_missing_file_is_rejected(self):
        plan = (task("u", kind="unit_test", text="piton/design/chip/tile/pico/rtl/nothing.v"),)
        problems = check_plan(spec("ariane", "Verify hello."), plan, path_exists=lambda p: False)
        assert rules(problems) == [(MISSING_MODULE, "u")]

    def test_a_unit_test_of_an_existing_file_passes(self):
        seen = []
        plan = (task("u", kind="unit_test", text="`piton/design/x.v`"),)
        assert check_plan(spec("ariane", "Verify hello."), plan, path_exists=lambda p: seen.append(p) or True) == ()
        assert seen == ["piton/design/x.v"]

    def test_without_a_checkout_the_rule_is_skipped(self):
        plan = (task("u", kind="unit_test", text="no/such.v"),)
        assert check_plan(spec("ariane", "Verify hello."), plan) == ()


class TestRepeatedFailure:
    def failed(self, s, t, note="task cfg_2x2_pico_mesh in iteration 1 (sim timeout)"):
        return {config_key(s, t): note}

    def test_a_task_that_rebuilds_a_failed_configuration_is_rejected(self):
        s = spec("ariane", "Verify hello.")
        plan = (task("again"),)
        problems = check_plan(s, plan, failed_configs=self.failed(s, task("old")))
        assert rules(problems) == [(REPEATED_FAILURE, "again")]
        assert "iteration 1" in problems[0].reason

    def test_a_different_configuration_passes(self):
        s = spec("ariane", "Verify hello.")
        plan = (task("new", config_rtl=("PITON_FPGA_SYNTH",)),)
        assert check_plan(s, plan, failed_configs=self.failed(s, task("old"))) == ()

    def test_a_task_that_builds_on_rtl_edits_is_not_a_repeat(self):
        s = spec("ariane", "Verify hello.")
        plan = (task("fix", kind="rtl", text="Fix it"), task("after", kind="workload", deps=("fix",)))
        failed = self.failed(s, task("old"))
        assert check_plan(s, plan, rtl_edits=True, failed_configs=failed) == ()

    def test_with_nothing_failed_the_rule_is_idle(self):
        assert check_plan(spec("ariane", "Verify hello."), (task("a"),), failed_configs={}) == ()


def test_a_normal_plan_for_a_normal_objective_has_no_problem():
    s = spec("ariane", "Verify the barrier_atomic.c gate workload passes on a 2x2 mesh.", ("barrier_atomic.c",))
    plan = (
        task("t1", text="Configure a 2x2 OpenPiton/Ariane mesh"),
        task("t2", kind="workload", deps=("t1",), text="Run barrier_atomic.c on the 2x2 mesh"),
    )
    assert check_plan(s, plan, path_exists=lambda p: False, failed_configs={}) == ()


def test_format_rejection_lists_every_reason_and_says_no_build_was_spent():
    problems = (plan_check.PlanProblem(SOURCE_EDIT, "a", "first reason."), plan_check.PlanProblem(WAY_RULE, "b", "second."))
    text = format_rejection(problems)
    assert "rejected before any build" in text
    assert "- first reason." in text and "- second." in text


class TestDefineIndex:
    def make_checkout(self, tmp_path):
        files = {
            "piton/design/chip/x.v": "`ifdef FOO_BAR\n`endif\nmodule m; endmodule",
            "piton/verif/env/top.v.pyv": "`ifndef MINIMAL_MONITORING\n",
            "piton/tools/src/sims/sims,2.0": "$ENV{PITON_FPGA_SYNTH}",
            "piton/design/build/gen.v": "`ifdef IN_BUILD_DIR\n",
            "piton/design/dump.vcd": "SKIPPED_SUFFIX",
            "piton/other/y.v": "`ifdef OUTSIDE_THE_DIRS\n",
        }
        for name, text in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return str(tmp_path)

    def test_words_in_the_scanned_directories_are_indexed(self, tmp_path):
        define_index.cache_clear()
        words = define_index(self.make_checkout(tmp_path))
        assert {"FOO_BAR", "MINIMAL_MONITORING", "PITON_FPGA_SYNTH"} <= words

    def test_a_define_is_found_wherever_a_file_spells_it(self, tmp_path):
        define_index.cache_clear()
        assert "PITON_FPGA_SYNTH" in define_index(self.make_checkout(tmp_path))  # a script, not an ifdef

    def test_skipped_directories_suffixes_and_other_trees_are_left_out(self, tmp_path):
        define_index.cache_clear()
        words = define_index(self.make_checkout(tmp_path))
        assert not {"IN_BUILD_DIR", "SKIPPED_SUFFIX", "OUTSIDE_THE_DIRS"} & words

    def test_a_missing_checkout_indexes_nothing(self, tmp_path):
        define_index.cache_clear()
        assert define_index(str(tmp_path / "nope")) == frozenset()
