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


class TestKnownBadChange:
    FPGA = "PITON_FPGA_SYNTH"

    def bad(self, *changes):
        return [tuple(changes)]

    def test_a_task_that_makes_a_change_probes_showed_breaks_the_design_is_rejected(self):
        from mace.narrowing import ADDED_DEFINE, Change

        s = spec("ariane", "Verify hello.")
        # A re-plan that keeps the seeded define beside a new change is no exact repeat of the failed build.
        plan = (task("again", config_rtl=(self.FPGA,), caches=(("l2", (32768, 4)),)),)
        problems = check_plan(s, plan, bad_changes=self.bad(Change(ADDED_DEFINE, self.FPGA)))
        assert rules(problems) == [(plan_check.KNOWN_BAD_CHANGE, "again")]
        assert "a probe earlier in this run" in problems[0].reason
        assert "leave out the RTL define PITON_FPGA_SYNTH" in problems[0].reason

    def test_a_task_without_the_change_passes(self):
        from mace.narrowing import ADDED_DEFINE, Change

        s = spec("ariane", "Verify hello.")
        plan = (task("t", caches=(("l2", (32768, 4)),)),)
        assert check_plan(s, plan, bad_changes=self.bad(Change(ADDED_DEFINE, self.FPGA))) == ()

    def test_a_combination_is_rejected_only_when_every_change_is_made(self):
        from mace.narrowing import ADDED_DEFINE, CACHE, Change

        s = spec("ariane", "Verify hello.")
        bad = self.bad(Change(ADDED_DEFINE, self.FPGA), Change(CACHE, "l2", (32768, 4)))
        both = (task("both", config_rtl=(self.FPGA,), caches=(("l2", (32768, 4)),)),)
        one = (task("one", config_rtl=(self.FPGA,)),)
        assert rules(check_plan(s, both, bad_changes=bad)) == [(plan_check.KNOWN_BAD_CHANGE, "both")]
        assert check_plan(s, one, bad_changes=bad) == ()

    def test_a_dropped_required_define_is_a_bad_change_a_task_must_not_repeat(self):
        from mace.narrowing import MISSING_DEFINE, Change

        # drop_bist breaks the build by leaving the define out. R2 stops a plan from leaving it out too;
        # this stops a task that drops a default define the probes showed is needed.
        s = spec("ariane", "Verify hello.")
        plan = (Task(id="t", deps=(), kind="config", spec="s", drop_rtl=("MINIMAL_MONITORING",)),)
        problems = check_plan(s, plan, bad_changes=self.bad(Change(MISSING_DEFINE, "MINIMAL_MONITORING")))
        assert rules(problems) == [(plan_check.KNOWN_BAD_CHANGE, "t")]
        assert "keep the RTL define MINIMAL_MONITORING" in problems[0].reason

    def test_a_define_the_objective_requires_is_no_change_so_it_is_never_flagged(self):
        from mace.narrowing import ADDED_DEFINE, Change

        plan = (task("t", config_rtl=(BIST,)),)
        assert check_plan(spec(), plan, bad_changes=self.bad(Change(ADDED_DEFINE, BIST))) == ()

    def test_a_task_that_builds_on_rtl_edits_is_not_checked(self):
        from mace.narrowing import ADDED_DEFINE, Change

        s = spec("ariane", "Verify hello.")
        plan = (task("fix", kind="rtl", text="Fix it", config_rtl=(self.FPGA,)),)
        assert check_plan(s, plan, rtl_edits=True, bad_changes=self.bad(Change(ADDED_DEFINE, self.FPGA))) == ()


class TestWrapperSkeleton:
    WRAPPER = "piton/design/chip/tile/serv/generic_core_wrapper.v"
    SPEC = spec(objective="Verify addi.S on a 2x2 mesh.")

    def test_a_plan_with_no_rtl_task_is_rejected_while_the_wrapper_is_a_skeleton(self):
        plan = (task("c1"), task("w1", kind="workload", deps=("c1",)))
        problems = check_plan(self.SPEC, plan, rtl_edits=True, unwritten_wrapper=self.WRAPPER)
        assert rules(problems) == [(plan_check.WRAPPER_SKELETON, ""), (plan_check.WRAPPER_SKELETON, "c1"),
                                   (plan_check.WRAPPER_SKELETON, "w1")]
        assert self.WRAPPER in problems[0].reason and "rtl task" in problems[0].reason

    def test_a_plan_whose_build_tasks_follow_an_rtl_task_passes(self):
        plan = (task("r1", kind="rtl", text="Write the wrapper body"), task("w1", kind="workload", deps=("r1",)),
                task("w2", kind="workload", deps=("w1",)))
        assert check_plan(self.SPEC, plan, rtl_edits=True, unwritten_wrapper=self.WRAPPER) == ()

    def test_a_config_task_before_the_rtl_task_is_rejected(self):
        # The Ibex runs of generic_v5: "add ibex_pkg.sv to the build configuration" ran first and timed out,
        # so the rtl task that depended on it never ran.
        plan = (task("01", text="Add ibex_pkg.sv to the build configuration"),
                task("02", kind="rtl", text="Write the wrapper body", deps=("01",)))
        problems = check_plan(self.SPEC, plan, rtl_edits=True, unwritten_wrapper=self.WRAPPER)
        assert rules(problems) == [(plan_check.WRAPPER_SKELETON, "01")]
        assert "depend on an rtl task" in problems[0].reason

    def test_a_build_task_beside_the_rtl_task_is_rejected(self):
        plan = (task("r1", kind="rtl", text="Write the wrapper body"), task("c1"))
        assert rules(check_plan(self.SPEC, plan, rtl_edits=True, unwritten_wrapper=self.WRAPPER)) == [
            (plan_check.WRAPPER_SKELETON, "c1")]

    def test_no_rule_once_the_wrapper_is_written_or_without_rtl_edits(self):
        assert check_plan(self.SPEC, (task("c1"),), rtl_edits=True) == ()
        assert check_plan(self.SPEC, (task("c1"),), unwritten_wrapper=self.WRAPPER) == ()


class TestNoteFailedConfig:
    def result(self, passed=False, build_ok=True, verdict="timeout", failure_reason="", edits=None, config=None):
        from chia.base.llm_call import QueryResult
        from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
        from mace.spec import StepResult

        build = PitonBuildArtifact(
            success=build_ok, returncode=0 if build_ok else 1, config=config or PitonConfig(), sim_type="vlt",
            model_dir="/x", binary_path="/x/V", wall_time_s=1.0, failure_reason=failure_reason,
        )
        run = PitonRunResult(success=passed, returncode=0, test="a.c", sim_type="vlt", run_dir="/x", verdict=verdict) if build_ok else None
        query = QueryResult(result="", returncode=0, stderr="", stream_result="", success=True)
        return StepResult(task=task("t9"), query=query, build=build, run=run, passed=passed, edits=edits)

    def test_a_failed_simulation_is_noted_with_its_verdict(self):
        failed = {}
        plan_check.note_failed_config(failed, self.result(), 0)
        ((flags, note),) = failed.items()
        assert "-sys=manycore" in flags and note == "task t9 in iteration 1 (its simulation ended timeout)"

    def test_a_failed_build_is_noted_as_a_build_failure(self):
        failed = {}
        plan_check.note_failed_config(failed, self.result(build_ok=False), 2)
        assert list(failed.values()) == ["task t9 in iteration 3 (its build failed)"]

    def test_a_pass_is_not_noted(self):
        failed = {}
        plan_check.note_failed_config(failed, self.result(passed=True, verdict="pass"), 0)
        assert failed == {}

    def test_a_build_the_run_never_started_is_not_noted(self):
        failed = {}
        plan_check.note_failed_config(failed, self.result(build_ok=False, failure_reason="budget_exhausted"), 0)
        assert failed == {}

    def test_a_build_with_rtl_edits_is_not_noted(self):
        failed = {}
        plan_check.note_failed_config(failed, self.result(edits={"a.v": "text"}), 0)
        assert failed == {}

    def test_the_first_failure_of_a_configuration_is_the_one_kept(self):
        failed = {}
        plan_check.note_failed_config(failed, self.result(), 0)
        plan_check.note_failed_config(failed, self.result(), 1)
        assert list(failed.values()) == ["task t9 in iteration 1 (its simulation ended timeout)"]


def test_a_normal_plan_for_a_normal_objective_has_no_problem():
    s = spec("ariane", "Verify the barrier_atomic.c gate workload passes on a 2x2 mesh.", ("barrier_atomic.c",))
    plan = (
        task("t1", text="Configure a 2x2 OpenPiton/Ariane mesh"),
        task("t2", kind="workload", deps=("t1",), text="Run barrier_atomic.c on the 2x2 mesh"),
    )
    assert check_plan(s, plan, path_exists=lambda p: False, failed_configs={}) == ()


class TestFormatUnusableReply:
    def test_it_quotes_the_error_and_gives_the_line_format(self):
        note = plan_check.format_unusable_reply("no TASK: lines in the planner's response: 'TASK: a | deps= | workload | x'")
        assert "could not be read as a plan: no TASK: lines" in note and "'TASK: a | deps= | workload | x'" in note
        assert "TASK: <id> | deps=<comma-separated task ids, or empty> | kind=<config|workload|unit_test> |" in note
        assert "the third starts with kind=" in note

    def test_the_rtl_kind_is_offered_only_in_a_run_with_rtl_edits(self):
        assert "|rtl>" not in plan_check.format_unusable_reply("e")
        assert "kind=<config|workload|unit_test|rtl>" in plan_check.format_unusable_reply("e", rtl_edits=True)

    def test_a_long_error_is_cut(self):
        assert len(plan_check.format_unusable_reply("x" * 5000)) < 700


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
