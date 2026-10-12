"""Tier-0 tests for mace.eval.directed and its runner methods.

Run:
    pytest mace/test/test_eval_directed.py -q

The builds and simulations are replaced by a fake integrate_parallel that
answers from a table of verdicts, keyed by whether the bug is applied.
"""

from __future__ import annotations

import pytest
from chia.base.llm_call import QueryResult

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics
from mace.eval import directed as ed
from mace.eval import runner
from mace.eval.source_faults import SourceEdit
from mace.eval.suite import SuiteTask
from mace.spec import Budget, DirectedTest, LoopResult, MaceSpec, StepResult, Task
from mace.test.conftest import FakeLLM

TARGET = "piton/design/chip/tile/pico/rtl/picorv32.v"
EXPERT = Task(id="expert", deps=(), kind="config", spec="known passing configuration", config_rtl=("CONFIG_DISABLE_BIST_CLEAR",))


def make_spec(**override):
    kwargs = {"workloads": ("sh.S",), "objective": "make sh.S pass", "core": "pico", "target_mesh": (2, 2)}
    kwargs.update(override)
    return MaceSpec(**kwargs)


def a_test(name, verdict="fail", attempt=1):
    return DirectedTest(name, TARGET, "S", f"source of {name}\n", attempt=attempt, verdict=verdict, wall_s=1.5)


def step(task, passed=True, build_ok=True, verdicts=(), tests=()):
    config = PitonConfig()
    build = PitonBuildArtifact(
        success=build_ok, returncode=0 if build_ok else 1, config=config, sim_type="vlt", model_dir="/x",
        binary_path="/x/V", wall_time_s=1.0, failure_reason="" if build_ok else "build_error",
    )
    runs = tuple(
        PitonRunResult(
            success=v == "pass", returncode=0, test=f"p{i}", sim_type="vlt", run_dir="/x", verdict=None if v == "none" else v,
            wall_time_s=2.0, sim_log_tail=f"log {v}",
        )
        for i, v in enumerate(verdicts)
    )
    query = QueryResult(result="ok", returncode=0, stderr="", stream_result="", success=True)
    return StepResult(
        task=task, query=query, build=build, run=runs[-1] if runs else None, passed=passed, runs=runs, tests=tests
    )


class TestClassify:
    @pytest.mark.parametrize(
        "fault,clean,outcome",
        [
            ("fail", "pass", "detected"),
            ("timeout", "pass", "detected"),
            ("maxcycles", "pass", "detected"),
            ("pass", "pass", "missed"),
            ("fail", "fail", "false_alarm"),
            ("pass", "fail", "false_alarm"),
            ("pass", "timeout", "false_alarm"),
            ("not_built", "pass", "unchecked"),
            ("not_built", "fail", "false_alarm"),
            ("fail", "no_verdict", "invalid"),
            ("pass", "not_built", "invalid"),
            ("no_verdict", "pass", "invalid"),
        ],
    )
    def test_the_outcome(self, fault, clean, outcome):
        assert ed.classify(fault, clean) == outcome

    def test_the_outcomes_are_the_ones_a_report_lists(self):
        assert set(ed.OUTCOMES) == {"detected", "missed", "false_alarm", "invalid", "unchecked"}


class TestTestFirst:
    def plan(self):
        return (
            Task(id="cfg", deps=(), kind="config", spec="x_tiles=2"),
            Task(
                id="fix", deps=("cfg",), kind="rtl", spec="fix the load", caches=(("l1d", (8192, 4)),),
                config_rtl=("CONFIG_DISABLE_BIST_CLEAR",), network="2dmesh_config",
            ),
            Task(id="run", deps=("fix",), kind="workload", spec="sh.S"),
        )

    def test_a_test_task_goes_before_each_rtl_task(self):
        out = ed.test_first()(0, self.plan())
        assert [(t.id, t.kind) for t in out] == [
            ("cfg", "config"), ("test_fix", "test"), ("fix", "rtl"), ("run", "workload"),
        ]

    def test_the_test_task_tests_the_design_the_rtl_task_will_change(self):
        test, rtl = ed.test_first()(0, self.plan())[1:3]
        original = self.plan()[1]
        assert test.deps == original.deps and test.caches == original.caches
        assert (test.config_rtl, test.network) == (original.config_rtl, original.network)
        assert "task fix changes: fix the load" in test.spec
        assert rtl.deps == ("cfg", "test_fix") and rtl.spec == original.spec

    def test_a_plan_without_rtl_tasks_is_unchanged(self):
        plan = (self.plan()[0], self.plan()[2])
        assert ed.test_first()(0, plan) == plan

    def test_every_iteration_gets_a_test_and_two_rtl_tasks_get_two(self):
        plan = (
            Task(id="a", deps=(), kind="rtl", spec="one"),
            Task(id="b", deps=("a",), kind="rtl", spec="two"),
        )
        out = ed.test_first()(2, plan)
        assert [t.id for t in out] == ["test_a", "a", "test_b", "b"]
        assert out[3].deps == ("a", "test_b")

    def test_the_test_id_avoids_a_clash(self):
        plan = (
            Task(id="test_fix", deps=(), kind="config", spec="x"),
            Task(id="fix", deps=(), kind="rtl", spec="y"),
        )
        out = ed.test_first()(0, plan)
        assert len({t.id for t in out}) == 3 and out[1].id == "test_fix_"


def record_tests(db, run_id, *tests, iteration=1, task_id="t"):
    result = step(Task(id=task_id, deps=(), kind="test", spec="s"), tests=tests)
    metrics.record_iteration(db, run_id, iteration, (result,), wall_s=1.0)


class TestChecking:
    @pytest.fixture
    def db(self, tmp_path):
        return metrics.open_db(str(tmp_path / "m.db"), ray_placement=False)

    @pytest.fixture
    def design(self, monkeypatch, tmp_path):
        """A fake design: ``design.verdicts[phase][program]``, with the phase
        flipping from ``bug`` to ``clean`` when the checkout is restored."""

        class Design:
            phase = "bug"
            verdicts = {"bug": {}, "clean": {}}
            events: list = []
            builds = {"bug": True, "clean": True}

        d = Design()
        d.events = []

        def fake_integrate(roots, spec, tasks, llm, asm_diag_root=None, nodes=None, options=None, **kw):
            program = spec.workloads[0]
            d.events.append((d.phase, program, asm_diag_root))
            ok = d.builds[d.phase]
            return (step(tasks[0], build_ok=ok, verdicts=(d.verdicts[d.phase].get(program, "pass"),) if ok else ()),)

        def fake_restore(root):
            d.events.append(("restore", root))
            d.phase = "clean"

        monkeypatch.setattr(ed, "integrate_parallel", fake_integrate)
        monkeypatch.setattr(ed, "open_nodes", lambda roots: [object()])
        monkeypatch.setattr(ed, "close_nodes", lambda nodes: d.events.append(("closed",)))
        monkeypatch.setattr("mace.eval.source_faults.restore", fake_restore)
        monkeypatch.setattr("mace.eval.runner.clear_build_cache", lambda root: d.events.append(("cleared", root)))
        d.root = str(tmp_path / "piton")
        return d

    def test_the_tests_of_a_run_are_read_back_from_the_database(self, db):
        run_id = metrics.start_run(db, make_spec())
        record_tests(db, run_id, a_test("one"), a_test("two", attempt=2), iteration=1)
        record_tests(db, run_id, a_test("three"), iteration=3, task_id="u")
        keys = [k for k, _ in ed.tests_of_run(db, run_id)]
        assert keys == [(1, "t", 1, "one"), (1, "t", 2, "two"), (3, "u", 1, "three")]
        assert ed.tests_of_run(db, "nothing") == []
        test = ed.tests_of_run(db, run_id)[0][1]
        assert (test.source, test.target, test.verdict, test.wall_s) == ("source of one\n", TARGET, "fail", 1.5)

    def test_each_test_runs_with_the_bug_then_without_and_is_classified(self, db, design):
        run_id = metrics.start_run(db, make_spec())
        record_tests(db, run_id, a_test("good"), a_test("blind"), a_test("wrong"))
        design.verdicts = {
            "bug": {"dt_good.S": "fail", "dt_blind.S": "pass", "dt_wrong.S": "fail"},
            "clean": {"dt_good.S": "pass", "dt_blind.S": "pass", "dt_wrong.S": "fail"},
        }

        checks = ed.check_tests((design.root, "/b"), make_spec(), EXPERT, db, run_id)

        assert {c.name: c.outcome for c in checks} == {"good": "detected", "blind": "missed", "wrong": "false_alarm"}
        order = [e[0] for e in design.events]
        assert order == ["bug"] * 3 + ["restore", "cleared"] + ["clean"] * 3 + ["closed"]
        rows = db.query("SELECT name, fault_verdict, clean_verdict, outcome FROM test_checks ORDER BY name")
        assert rows == [
            {"name": "blind", "fault_verdict": "pass", "clean_verdict": "pass", "outcome": "missed"},
            {"name": "good", "fault_verdict": "fail", "clean_verdict": "pass", "outcome": "detected"},
            {"name": "wrong", "fault_verdict": "fail", "clean_verdict": "fail", "outcome": "false_alarm"},
        ]

    def test_only_the_first_checkout_is_used_and_restored(self, db, design):
        run_id = metrics.start_run(db, make_spec())
        record_tests(db, run_id, a_test("good"))
        ed.check_tests((design.root, "/b"), make_spec(), EXPERT, db, run_id)
        assert ("restore", design.root) in design.events and ("cleared", design.root) in design.events
        assert not any(e[0] == "restore" and e[1] == "/b" for e in design.events)

    def test_each_test_runs_alone_from_a_directory_of_its_own(self, db, design, tmp_path):
        run_id = metrics.start_run(db, make_spec())
        record_tests(db, run_id, a_test("one"), a_test("two"))
        ed.check_tests((str(tmp_path),), make_spec(), EXPERT, db, run_id)
        dirs = {e[1]: e[2] for e in design.events if len(e) == 3 and e[0] == "bug"}
        assert dirs["dt_one.S"] != dirs["dt_two.S"]
        assert (tmp_path / "build" / ".mace_tests" / "check" / "0" / "dt_one.S").read_text() == "source of one\n"
        assert dirs["dt_one.S"] == str(tmp_path / "build" / ".mace_tests" / "check" / "0")

    def test_a_bug_that_breaks_the_build_leaves_the_tests_unchecked(self, db, design):
        run_id = metrics.start_run(db, make_spec())
        record_tests(db, run_id, a_test("fine"), a_test("bad"))
        design.builds["bug"] = False
        design.verdicts["clean"] = {"dt_bad.S": "fail"}
        checks = ed.check_tests((design.root,), make_spec(), EXPERT, db, run_id)
        assert {c.name: (c.fault_verdict, c.outcome) for c in checks} == {
            "fine": ("not_built", "unchecked"), "bad": ("not_built", "false_alarm"),
        }

    def test_a_run_with_no_tests_checks_nothing(self, db, design):
        run_id = metrics.start_run(db, make_spec())
        assert ed.check_tests((design.root,), make_spec(), EXPERT, db, run_id) == []
        assert design.events == []

    def test_the_seconds_cover_both_designs(self, db, design):
        run_id = metrics.start_run(db, make_spec())
        record_tests(db, run_id, a_test("good"))
        (check,) = ed.check_tests((design.root,), make_spec(), EXPERT, db, run_id)
        assert check.wall_s >= 0 and check.iteration == 1 and check.attempt == 1


class TestTestgen:
    @pytest.fixture
    def db(self, tmp_path):
        return metrics.open_db(str(tmp_path / "m.db"), ray_placement=False)

    @pytest.fixture
    def calls(self, monkeypatch):
        calls = []

        def fake_integrate(roots, spec, tasks, llm, tools=(), nodes=None, rtl_context="", run_id=None, iteration=0, **kw):
            calls.append({"iteration": iteration, "task": tasks[0], "context": rtl_context, "roots": roots})
            if iteration == 0:
                return (step(tasks[0], passed=False, verdicts=("fail",)),)
            return (step(tasks[0], tests=(a_test("lh_sign"),), verdicts=("fail",)),)

        monkeypatch.setattr(ed, "integrate_parallel", fake_integrate)
        monkeypatch.setattr(ed, "open_nodes", lambda roots: ["node"])
        monkeypatch.setattr(ed, "close_nodes", lambda nodes: calls.append("closed"))
        return calls

    def run(self, db, outcomes, monkeypatch):
        checks = [ed.TestCheck(1, "test", 1, f"t{i}", "fail", "pass", o) for i, o in enumerate(outcomes)]
        monkeypatch.setattr(ed, "check_tests", lambda roots, spec, expert, db_, run_id: checks)
        return ed.run_testgen(("/a", "/b"), make_spec(), EXPERT, FakeLLM([]), db, labels=metrics.RunLabels(method="testgen", task="t"))

    def test_the_run_passes_when_a_test_was_detected(self, db, calls, monkeypatch):
        result = self.run(db, ["missed", "detected"], monkeypatch)
        assert result.status == "passed"
        assert db.query_value("SELECT status FROM runs WHERE run_id = ?", (result.run_id,)) == "passed"
        assert db.query_value("SELECT method FROM runs WHERE run_id = ?", (result.run_id,)) == "testgen"

    @pytest.mark.parametrize("outcomes", [[], ["missed"], ["false_alarm", "invalid", "unchecked"]])
    def test_the_run_fails_when_none_was(self, db, calls, monkeypatch, outcomes):
        assert self.run(db, outcomes, monkeypatch).status == "failed"

    def test_iteration_0_runs_the_gates_and_iteration_1_writes_tests_with_that_evidence(self, db, calls, monkeypatch):
        self.run(db, ["detected"], monkeypatch)
        first, second = [c for c in calls if c != "closed"]
        assert first["iteration"] == 0 and first["task"] is EXPERT and first["roots"] == ("/a",)
        assert second["iteration"] == 1 and second["task"].kind == "test"
        assert second["context"].startswith("The gate workloads fail on this design:")
        assert "Build flags:" in second["context"]
        task = second["task"]
        assert (task.caches, task.config_rtl, task.network) == (EXPERT.caches, EXPERT.config_rtl, EXPERT.network)
        assert "bug under piton/design/" in task.spec and calls[-1] == "closed"

    def test_the_context_says_so_when_the_gates_pass(self, db, calls, monkeypatch):
        def passing(roots, spec, tasks, llm, tools=(), nodes=None, rtl_context="", run_id=None, iteration=0, **kw):
            calls.append({"iteration": iteration, "context": rtl_context})
            return (step(tasks[0], verdicts=("pass",)),)

        monkeypatch.setattr(ed, "integrate_parallel", passing)
        self.run(db, ["missed"], monkeypatch)
        assert "pass on this design, so the bug is not one they expose" in [c for c in calls if c != "closed"][1]["context"]

    def test_both_iterations_are_recorded(self, db, calls, monkeypatch):
        result = self.run(db, ["detected"], monkeypatch)
        rows = db.query("SELECT iteration, num_tasks FROM iterations WHERE run_id = ? ORDER BY iteration", (result.run_id,))
        assert rows == [{"iteration": 0, "num_tasks": 1}, {"iteration": 1, "num_tasks": 1}]
        assert db.query_value("SELECT COUNT(*) FROM directed_tests WHERE run_id = ?", (result.run_id,)) == 1
        assert len(result.iterations) == 2

    def test_an_exception_ends_the_run_as_an_error(self, db, calls, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("cluster gone")

        monkeypatch.setattr(ed, "integrate_parallel", boom)
        with pytest.raises(RuntimeError):
            self.run(db, [], monkeypatch)
        assert db.query_value("SELECT status FROM runs") == "error"
        assert calls == ["closed"]


class TestRunnerMethods:
    def planted(self, **override):
        kwargs = dict(
            id="pico-2x2-sh-rtlbug", core="pico", mesh=(2, 2), workloads=("sh.S",), objective="Verify it.",
            budget=Budget(max_iterations=3), verified=False,
            source_fault=(SourceEdit("piton/design/x.v", "a", "b"),),
        )
        kwargs.update(override)
        return SuiteTask(**kwargs)

    def env(self, tmp_path):
        db = metrics.open_db(str(tmp_path / "m.db"), ray_placement=False)
        return runner.RunEnv(piton_roots=("/a", "/b"), llm=FakeLLM([]), db=db, meta={}, clear_cache=False)

    @pytest.fixture(autouse=True)
    def quiet(self, monkeypatch):
        from contextlib import nullcontext

        monkeypatch.setattr(runner, "faulted", lambda roots, edits: nullcontext())
        monkeypatch.setattr(runner, "restore", lambda root: None)

    def test_both_methods_run_only_on_tasks_with_a_source_fault(self):
        plain = self.planted(id="plain", source_fault=())
        jobs = runner.plan_jobs((plain, self.planted()), ("testgen", "mace_rtl_test"), repeats=2)
        assert sorted(j.key for j in jobs) == [
            ("pico-2x2-sh-rtlbug", "mace_rtl_test", 0), ("pico-2x2-sh-rtlbug", "mace_rtl_test", 1),
            ("pico-2x2-sh-rtlbug", "testgen", 0), ("pico-2x2-sh-rtlbug", "testgen", 1),
        ]

    def test_both_need_an_llm(self):
        assert not {"testgen", "mace_rtl_test"} & runner.NO_LLM_METHODS

    def test_testgen_runs_on_one_checkout_under_the_bug(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(
            runner, "run_testgen",
            lambda roots, spec, expert, llm, db, labels=None: seen.append((roots, expert.id, labels)) or "done",
        )
        job = runner.Job(self.planted(), "testgen", 3)
        assert runner.run_job(job, self.env(tmp_path)) == "done"
        ((roots, expert_id, labels),) = seen
        assert roots == ("/a",) and expert_id == "expert"
        assert (labels.method, labels.task, labels.repeat) == ("testgen", "pico-2x2-sh-rtlbug", 3)
        assert labels.meta["source_fault"] == ["piton/design/x.v"]

    def test_mace_rtl_test_runs_the_loop_with_rtl_edits_and_the_test_first_hook_then_checks(self, tmp_path, monkeypatch):
        seen = []

        def loop(roots, spec, llm, db, labels=None, options=None, plan_hook=None):
            seen.append(("loop", roots, options, plan_hook))
            return LoopResult(run_id="r1", status="passed", iterations=())

        monkeypatch.setattr(runner, "run_mace_loop", loop)
        monkeypatch.setattr(
            runner, "check_tests", lambda roots, spec, expert, db, run_id: seen.append(("check", roots, expert.id, run_id))
        )
        result = runner.run_job(runner.Job(self.planted(), "mace_rtl_test", 0), self.env(tmp_path))
        assert result.status == "passed"
        (_, roots, options, hook), check = seen
        assert roots == ("/a", "/b") and options.rtl_edits is True and options.task_prompts is True
        plan = (Task(id="fix", deps=(), kind="rtl", spec="fix it"),)
        assert [t.kind for t in hook(0, plan)] == ["test", "rtl"]
        assert check == ("check", ("/a", "/b"), "expert", "r1")

    def test_the_directed_tests_of_an_earlier_job_are_cleared_with_the_build_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "run_expert", lambda *a, **kw: "done")
        (tmp_path / "build" / ".mace_tests" / "old").mkdir(parents=True)
        (tmp_path / "build" / ".mace_tests" / "old" / "dt_x.S").write_text("x")
        db = metrics.open_db(str(tmp_path / "m.db"), ray_placement=False)
        env = runner.RunEnv(piton_roots=(str(tmp_path),), llm=None, db=db, meta={}, clear_cache=True)
        runner.run_job(runner.Job(self.planted(source_fault=()), "expert", 0), env)
        assert not (tmp_path / "build" / ".mace_tests").exists()
