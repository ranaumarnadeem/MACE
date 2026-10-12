"""Tier-0 tests for mace.eval.directed_report.

Run:
    pytest mace/test/test_eval_directed_report.py -q

Each test builds a small database with the real recording functions, then
reads the tables back.
"""

from __future__ import annotations

import pytest
from chia.base.llm_call import QueryResult

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics
from mace.eval import directed_report as dr
from mace.spec import DirectedTest, MaceSpec, StepResult, Task
from mace.usage import LLMCall

BUG = "pico-2x2-sh-rtlbug"
OLD = "pico-2x2-addi-rtlfault"
FAULT_FILE = "piton/design/chip/tile/pico/rtl/picorv32.v"
OTHER_FILE = "piton/design/chip/tile/l2/rtl/l2_amo_alu.v"


def spec():
    return MaceSpec(workloads=("sh.S",), objective="make sh.S pass", core="pico", target_mesh=(2, 2))


def a_test(name, target=FAULT_FILE, attempt=1):
    return DirectedTest(name, target, "S", f"source of {name}\n", attempt=attempt, verdict="fail", wall_s=1.0)


def step(tests):
    build = PitonBuildArtifact(
        success=True, returncode=0, config=PitonConfig(), sim_type="vlt", model_dir="/x", binary_path="/x/V",
        wall_time_s=1.0,
    )
    run = PitonRunResult(success=False, returncode=0, test="p", sim_type="vlt", run_dir="/x", verdict="fail")
    query = QueryResult(result="ok", returncode=0, stderr="", stream_result="", success=True)
    task = Task(id="test_fix", deps=(), kind="test", spec="s")
    return StepResult(task=task, query=query, build=build, run=run, passed=True, runs=(run,), tests=tests)


class Db:
    def __init__(self, tmp_path, name="vm.db"):
        self.path = str(tmp_path / name)
        self.db = metrics.open_db(self.path, ray_placement=False)

    def run(self, task, method, repeat, status, *, tests=(), outcomes=(), usd=0.01, wall_s=30.0, source_fault=None):
        meta = {"source_fault": [FAULT_FILE] if source_fault is None else source_fault}
        run_id = metrics.start_run(
            self.db, spec(), labels=metrics.RunLabels(method=method, task=task, repeat=repeat, meta=meta)
        )
        if tests:
            metrics.record_iteration(self.db, run_id, 1, (step(tests),), wall_s=wall_s)
        else:
            metrics.record_iteration(self.db, run_id, 1, (), wall_s=wall_s)
        metrics.record_llm_calls(self.db, run_id, 1, (LLMCall(phase="task", usd=usd),))
        for t, outcome in zip(tests, outcomes):
            fault_v, clean_v = {
                "detected": ("fail", "pass"), "missed": ("pass", "pass"), "false_alarm": ("fail", "fail"),
                "runtime_broken": ("timeout", "pass"),
            }[outcome]
            metrics.record_test_check(self.db, run_id, 1, "test_fix", t.attempt, t.name, fault_v, clean_v, outcome, 5.0)
        if outcomes:
            # check_tests records the null test's verdicts too; its row is not a test.
            metrics.record_test_check(self.db, run_id, -1, "null", 0, "null", "pass", "pass", "null", 3.0)
        metrics.finish_run(self.db, run_id, status)
        return run_id


@pytest.fixture
def vm(tmp_path):
    return Db(tmp_path)


class TestGroups:
    def test_the_three_groups(self):
        assert dr.group_of("ariane-2x2-hello-rtlfault") == "old"
        assert dr.group_of("ariane-2x2-barrier-amobug") == "held-out"
        assert dr.group_of("pico-2x2-bgeu-rtlbug") == "development"


class TestCheckRows:
    def test_each_test_gets_its_outcome_target_and_whether_it_hit_the_faulted_file(self, vm):
        vm.run(BUG, "testgen", 0, "passed", tests=(a_test("hit"), a_test("off", OTHER_FILE)), outcomes=("detected", "missed"))
        rows = dr.check_rows([vm.path])
        assert [(r["name"], r["outcome"], r["target_hit"], r["method"], r["task"]) for r in rows] == [
            ("hit", "detected", True, "testgen", BUG), ("off", "missed", False, "testgen", BUG),
        ]
        assert rows[0]["verdict"] == "fail" and rows[0]["fault_verdict"] == "fail" and rows[0]["clean_verdict"] == "pass"

    def test_a_run_a_later_run_replaced_is_left_out(self, vm):
        vm.run(BUG, "testgen", 0, "error", tests=(a_test("old"),), outcomes=("missed",))
        vm.run(BUG, "testgen", 0, "passed", tests=(a_test("new"),), outcomes=("detected",))
        assert [r["name"] for r in dr.check_rows([vm.path])] == ["new"]


class TestDetection:
    def build(self, vm):
        vm.run(BUG, "testgen", 0, "passed", tests=(a_test("a"), a_test("b", OTHER_FILE)), outcomes=("detected", "false_alarm"), usd=0.02, wall_s=40)
        vm.run(BUG, "testgen", 1, "failed", tests=(a_test("c"),), outcomes=("missed",), usd=0.04, wall_s=60)
        vm.run(BUG, "testgen", 2, "failed")
        vm.run(BUG, "mace_rtl_test", 0, "passed", tests=(a_test("d"),), outcomes=("detected",))
        vm.run(OLD, "testgen", 0, "failed", tests=(a_test("e"),), outcomes=("missed",))
        vm.run(BUG, "mace_rtl", 0, "passed")

    def test_one_row_per_task_and_detecting_method(self, vm):
        self.build(vm)
        rows = dr.detection_table([vm.path])
        assert [(r["group"], r["task"], r["method"]) for r in rows] == [
            ("held-out", BUG, "mace_rtl_test"), ("held-out", BUG, "testgen"), ("old", OLD, "testgen"),
        ]

    def test_the_counts_for_testgen(self, vm):
        self.build(vm)
        row = next(r for r in dr.detection_table([vm.path]) if (r["task"], r["method"]) == (BUG, "testgen"))
        assert (row["runs"], row["runs_with_tests"], row["tests"]) == (3, 2, 3)
        assert (row["detected"], row["missed"], row["false_alarm"], row["invalid"], row["unchecked"]) == (1, 1, 1, 0, 0)
        assert (row["runs_detected"], row["runs_false_alarm"]) == (1, 1)
        assert row["target_hit"] == pytest.approx(2 / 3)
        # The medians over the three runs: $0.02, $0.04, $0.01 and 40 s, 60 s, 30 s.
        assert row["usd"] == pytest.approx(0.02) and row["method_s"] == pytest.approx(40)

    def test_the_null_test_is_not_counted_as_a_test(self, vm):
        vm.run(BUG, "testgen", 0, "passed", tests=(a_test("a"),), outcomes=("detected",))
        (row,) = dr.detection_table([vm.path])
        assert (row["tests"], row["detected"], row["runs_with_tests"]) == (1, 1, 1)
        assert [r["name"] for r in dr.check_rows([vm.path])] == ["a"]
        assert sorted(r["name"] for r in dr.check_rows([vm.path], with_null=True)) == ["a", "null"]

    def test_tests_on_a_bug_that_fails_every_program_are_counted_apart(self, vm):
        vm.run(BUG, "testgen", 0, "failed", tests=(a_test("a"), a_test("b")), outcomes=("runtime_broken", "runtime_broken"))
        (row,) = dr.detection_table([vm.path])
        assert (row["tests"], row["runtime_broken"], row["detected"], row["runs_detected"]) == (2, 2, 0, 0)
        assert "runtime_broken" in dr.OUTCOMES and "runtime_broken" in dr.DETECTION_COLUMNS

    def test_a_method_with_no_runs_has_no_row(self, vm):
        vm.run(BUG, "mace_rtl", 0, "passed")
        assert dr.detection_table([vm.path]) == []

    def test_a_row_renders_with_every_column(self, vm):
        from mace.eval.report import markdown_table

        self.build(vm)
        text = markdown_table(dr.detection_table([vm.path]), dr.DETECTION_COLUMNS)
        assert text.splitlines()[0].startswith("| group | task | method | runs |")
        assert "| held-out | pico-2x2-sh-rtlbug | testgen | 3 | 2 | 3 | 1 | 1 | 1 |" in text


class TestRepair:
    def test_pass_counts_time_and_cost_per_pass(self, vm):
        vm.run(BUG, "mace_rtl", 0, "passed", usd=0.10)
        vm.run(BUG, "mace_rtl", 1, "failed", usd=0.30)
        vm.run(BUG, "mace_rtl_test", 0, "passed", usd=0.20)
        vm.run(BUG, "mace_rtl_test", 1, "passed", usd=0.40)
        vm.run(BUG, "testgen", 0, "passed")
        rows = {r["method"]: r for r in dr.repair_table([vm.path])}
        assert set(rows) == {"mace_rtl", "mace_rtl_test"}
        assert (rows["mace_rtl"]["runs"], rows["mace_rtl"]["passed"]) == (2, 1)
        assert rows["mace_rtl"]["usd_per_pass"] == pytest.approx(0.40)
        assert (rows["mace_rtl_test"]["runs"], rows["mace_rtl_test"]["passed"]) == (2, 2)
        assert rows["mace_rtl_test"]["usd_per_pass"] == pytest.approx(0.30)
        assert rows["mace_rtl"]["group"] == "held-out"

    def test_no_pass_means_no_cost_per_pass(self, vm):
        vm.run(BUG, "mace_rtl", 0, "failed")
        (row,) = dr.repair_table([vm.path])
        assert row["passed"] == 0 and row["usd_per_pass"] is None and row["time_to_pass_s"] is None


class TestTestFirst:
    def test_runs_that_wrote_a_detected_test_are_counted_apart(self, vm):
        vm.run(BUG, "mace_rtl_test", 0, "passed", tests=(a_test("a"),), outcomes=("detected",))
        vm.run(BUG, "mace_rtl_test", 1, "failed", tests=(a_test("b"),), outcomes=("detected",))
        vm.run(BUG, "mace_rtl_test", 2, "passed", tests=(a_test("c"),), outcomes=("missed",))
        vm.run(BUG, "mace_rtl_test", 3, "failed")
        vm.run(BUG, "mace_rtl", 0, "passed")
        (row,) = dr.test_first_table([vm.path])
        assert (row["runs"], row["passed"]) == (4, 2)
        assert (row["detected_runs"], row["detected_and_passed"]) == (2, 1)
        assert (row["other_runs"], row["other_and_passed"]) == (2, 1)


class TestSeveralDatabases:
    def test_runs_from_each_database_are_merged(self, tmp_path):
        a, b = Db(tmp_path, "a.db"), Db(tmp_path, "b.db")
        a.run(BUG, "testgen", 0, "passed", tests=(a_test("x"),), outcomes=("detected",))
        b.run(BUG, "testgen", 1, "passed", tests=(a_test("y"),), outcomes=("detected",))
        (row,) = dr.detection_table([a.path, b.path])
        assert (row["runs"], row["tests"], row["runs_detected"]) == (2, 2, 2)
