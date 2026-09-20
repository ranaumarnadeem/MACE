"""Tier-0 tests for mace.orchestrator.run_mace_loop.

Run:
    pytest mace/test/test_orchestrator.py -q

run_mace_loop's own collaborators (mace.planner.plan, mace.integrator.
integrate_parallel) are real @ChiaFunction/Ray-dispatching code, so they are
monkeypatched here with plain fakes -- this file tests the loop's own control
flow (budget checks, status transitions, wall-time accounting, replan
feedback), not the real build/run pipeline underneath it (see
mace.test.cluster.orchestrator_e2e_test for that, against a live Ray
cluster).
"""

from __future__ import annotations

import time

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics
from mace.orchestrator import run_mace_loop
from mace.report import ReportError
from mace.spec import Budget, MaceSpec, StepResult, Task, Triage
from mace.test.conftest import FakeLLM


def _no_post_mortem(*args, **kwargs):
    """A generate_post_mortem stand-in matching orchestrator's own fail-open
    handling (`except ReportError: pass`) -- these control-flow tests aren't
    about report generation, which mace/test/test_report.py already covers.
    """
    raise ReportError("stubbed: no post-mortem needed for this control-flow test")


def make_spec(**override):
    kwargs = {
        "workloads": ("hello_world.c",),
        "objective": "bring up 1x1 ariane",
        "budget": Budget(max_iterations=1),
    }
    kwargs.update(override)
    return MaceSpec(**kwargs)


def make_db(tmp_path):
    return metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)


def step_result(task_id="t1", passed=True, verdict="pass", build_success=True):
    cfg = PitonConfig()
    build = PitonBuildArtifact(
        success=build_success, returncode=0 if build_success else 1, config=cfg,
        sim_type="vlt", model_dir="/x", binary_path="/x/Vcmp_top" if build_success else "",
        wall_time_s=1.0,
    )
    run = None
    if build_success:
        run = PitonRunResult(
            success=passed, returncode=0, test="hello_world.c", sim_type="vlt",
            run_dir="/x/runs/1", verdict=verdict,
        )
    task = Task(id=task_id, deps=(), kind="workload", spec="hello_world.c")
    query = FakeLLM(responses=["edit"]).prompt("edit")
    return StepResult(task=task, query=query, build=build, run=run, passed=passed)


def fake_plan(tasks_by_call):
    """A mace.planner.plan stand-in that returns one task DAG per call,
    consuming *tasks_by_call* in order."""
    calls = list(tasks_by_call)

    def _plan(spec, llm, tools=(), feedback=""):
        return calls.pop(0)

    return _plan


class TestIterationWallTimeIncludesPlanning:
    def test_iter_wall_s_includes_the_planner_call(self, tmp_path, monkeypatch):
        """The recorded wall_s -- and therefore the execution_time_s the
        paper/README cite -- must count the Planner's own LLM round-trip,
        not just integrate_parallel's, or it's measured on a different
        basis than baseline (b)'s comparable timer (which does include its
        one LLM call). See run_mace_loop's own comment on this.
        """
        db = make_db(tmp_path)
        planner_delay_s = 0.2

        def slow_plan(spec, llm, tools=(), feedback=""):
            time.sleep(planner_delay_s)
            return (Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)

        def fake_integrate_parallel(
            piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None
        ):
            return (step_result(tasks[0].id),)

        monkeypatch.setattr("mace.orchestrator.plan", slow_plan)
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", fake_integrate_parallel)

        result = run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db)

        assert result.status == "passed"
        recorded_wall_s = db.query_value(
            "SELECT wall_s FROM iterations WHERE run_id = ? AND iteration = 0",
            (result.run_id,),
        )
        assert recorded_wall_s >= planner_delay_s


class TestOnTaskProgressIsThreadedThrough:
    def test_reaches_integrate_parallel_as_given(self, tmp_path, monkeypatch):
        """run_mace_loop must pass its own on_task_progress straight through
        to integrate_parallel, the only place that can actually call it.
        """
        received = {}

        def capturing_integrate_parallel(
            piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None
        ):
            received["on_task_progress"] = on_task_progress
            return (step_result(tasks[0].id),)

        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)]),
        )
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", capturing_integrate_parallel)

        sentinel = lambda task_ids, stage: None  # noqa: E731
        run_mace_loop(
            ("/fake/root",), make_spec(), FakeLLM(responses=[]), make_db(tmp_path),
            on_task_progress=sentinel,
        )

        assert received["on_task_progress"] is sentinel


class TestStatusTransitions:
    def test_all_tasks_passing_stops_with_passed(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        monkeypatch.setattr("mace.orchestrator.plan", fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)]))
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None: (
                step_result("t1"),
            ),
        )

        result = run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db)

        assert result.status == "passed"
        assert len(result.iterations) == 1

    def test_planning_error_stops_with_planning_failed(self, tmp_path, monkeypatch):
        from mace.planner import PlanningError

        def raising_plan(spec, llm, tools=(), feedback=""):
            raise PlanningError("no TASK: lines in the planner's response")

        monkeypatch.setattr("mace.orchestrator.plan", raising_plan)

        result = run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db=make_db(tmp_path))

        assert result.status == "planning_failed"
        assert result.iterations == ()

    def test_exhausting_max_iterations_without_passing_is_budget_exceeded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)] * 2),
        )
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None: (
                step_result("t1", passed=False, verdict="fail"),
            ),
        )
        monkeypatch.setattr(
            "mace.orchestrator.triage",
            lambda result, llm, tools=(): Triage(diagnosis="rtl_suspect", fix="try a different mesh"),
        )
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)

        result = run_mace_loop(
            ("/fake/root",), make_spec(budget=Budget(max_iterations=2)), FakeLLM(responses=[]),
            db=make_db(tmp_path),
        )

        assert result.status == "budget_exceeded"
        assert len(result.iterations) == 2

    def test_a_failure_feeds_its_diagnosis_forward_as_the_next_plan_call_s_feedback(
        self, tmp_path, monkeypatch
    ):
        seen_feedback = []

        def recording_plan(spec, llm, tools=(), feedback=""):
            seen_feedback.append(feedback)
            return (Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)

        monkeypatch.setattr("mace.orchestrator.plan", recording_plan)
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None: (
                step_result("t1", passed=False, verdict="fail"),
            ),
        )
        monkeypatch.setattr(
            "mace.orchestrator.triage",
            lambda result, llm, tools=(): Triage(diagnosis="rtl_suspect", fix="try a different mesh"),
        )
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)

        run_mace_loop(
            ("/fake/root",), make_spec(budget=Budget(max_iterations=2)), FakeLLM(responses=[]),
            db=make_db(tmp_path),
        )

        assert seen_feedback[0] == ""  # nothing to feed back on the first call
        assert "rtl_suspect" in seen_feedback[1]
        assert "try a different mesh" in seen_feedback[1]
