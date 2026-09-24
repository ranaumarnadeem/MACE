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

import pytest
from chia.base.tools.ChiaTool import ChiaTool
from ray import cloudpickle

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from chia_openpiton.tools import PitonToolServer
from mace import metrics
from mace.orchestrator import run_mace_loop
from mace.report import ReportError
from mace.spec import Budget, MaceSpec, StepResult, Task, Triage
from mace.test.conftest import FakeLLM


@pytest.fixture(autouse=True)
def _stub_node_lifecycle(monkeypatch):
    """run_mace_loop builds real OpenPitonWorkspaceNode instances via
    mace.integrator.open_nodes to reuse across replan iterations -- these
    tests fake out integrate_parallel entirely and use a fake, nonexistent
    piton_roots, so open_nodes must not try to construct a real one either.
    close_nodes on the resulting empty list is a no-op, so it needs no
    separate stub.
    """
    monkeypatch.setattr("mace.orchestrator.open_nodes", lambda piton_roots: [])


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


def step_result(task_id="t1", passed=True, verdict="pass", build_success=True, run_dir="/x/runs/1"):
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
            run_dir=run_dir, verdict=verdict,
        )
    task = Task(id=task_id, deps=(), kind="workload", spec="hello_world.c")
    query = FakeLLM(responses=["edit"]).prompt("edit")
    return StepResult(task=task, query=query, build=build, run=run, passed=passed)


def failed_step_result(run_dir, sim_log):
    """A failed StepResult whose run_dir really exists and holds *sim_log*."""
    run_dir.mkdir()
    (run_dir / "sim.log").write_text(sim_log)
    return step_result("t1", passed=False, verdict="fail", run_dir=str(run_dir))


def fake_plan(tasks_by_call):
    """A mace.planner.plan stand-in that returns one task DAG per call,
    consuming *tasks_by_call* in order."""
    calls = list(tasks_by_call)

    def _plan(spec, llm, tools=(), feedback=""):
        return calls.pop(0)

    return _plan


def fake_integrate_parallel(results_by_iteration):
    """An integrate_parallel stand-in returning iteration i's one result,
    ``results_by_iteration[i]``."""

    def _integrate_parallel(
        piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None,
        nodes=None,
    ):
        return (results_by_iteration[iteration],)

    return _integrate_parallel


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
            piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None,
            nodes=None,
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
            piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None,
            nodes=None,
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


class TestNodeLifecycleAcrossIterations:
    def test_nodes_are_opened_once_and_closed_once_across_multiple_iterations(
        self, tmp_path, monkeypatch
    ):
        """Checkouts must not be re-acquired (a real Ray placement-group
        cycle) on every replan iteration -- piton_roots never changes
        within one run. See run_mace_loop's own comment on this.
        """
        open_calls = []
        close_calls = []

        def counting_open_nodes(piton_roots):
            open_calls.append(piton_roots)
            return ["node-stub"]

        monkeypatch.setattr("mace.orchestrator.open_nodes", counting_open_nodes)
        monkeypatch.setattr("mace.orchestrator.close_nodes", lambda nodes: close_calls.append(nodes))
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)] * 3),
        )
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None: (
                step_result("t1", passed=False, verdict="fail"),
            ),
        )
        monkeypatch.setattr(
            "mace.orchestrator.triage",
            lambda result, llm, tools=(): Triage(diagnosis="rtl_suspect", fix="try a different mesh"),
        )
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)

        run_mace_loop(
            ("/fake/root",), make_spec(budget=Budget(max_iterations=3)), FakeLLM(responses=[]),
            db=make_db(tmp_path),
        )

        assert len(open_calls) == 1  # not once per iteration
        assert len(close_calls) == 1  # closed exactly once, after the loop

    def test_planning_failure_before_any_iteration_never_opens_a_node(self, tmp_path, monkeypatch):
        """verify_checksums' own guarantee -- no checkout touched before a
        real task is about to run -- must extend to node construction too.
        """
        from mace.planner import PlanningError

        open_calls = []
        monkeypatch.setattr(
            "mace.orchestrator.open_nodes", lambda piton_roots: open_calls.append(piton_roots)
        )

        def raising_plan(spec, llm, tools=(), feedback=""):
            raise PlanningError("no TASK: lines in the planner's response")

        monkeypatch.setattr("mace.orchestrator.plan", raising_plan)

        run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db=make_db(tmp_path))

        assert open_calls == []


class TestStatusTransitions:
    def test_all_tasks_passing_stops_with_passed(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        monkeypatch.setattr("mace.orchestrator.plan", fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)]))
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None: (
                step_result("t1"),
            ),
        )

        result = run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db)

        assert result.status == "passed"
        assert len(result.iterations) == 1

    def test_unexpected_exception_records_error_and_propagates(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        monkeypatch.setattr("mace.orchestrator.plan", fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)]))

        def crashing_integrate_parallel(*args, **kwargs):
            raise RuntimeError("Duplicate function declaration found")

        monkeypatch.setattr("mace.orchestrator.integrate_parallel", crashing_integrate_parallel)

        with pytest.raises(RuntimeError, match="Duplicate function"):
            run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db)

        row = db.query("SELECT status, finished_at FROM runs")[0]
        assert row["status"] == "error"
        assert row["finished_at"] is not None

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
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None: (
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
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None: (
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

    def test_a_third_plan_call_sees_every_earlier_iteration_s_diagnosis_not_just_the_latest(
        self, tmp_path, monkeypatch
    ):
        seen_feedback = []
        diagnoses = iter([
            Triage(diagnosis="rtl_suspect", fix="try a different mesh"),
            Triage(diagnosis="build_timeout", fix="raise the sim wall clock"),
        ])

        def recording_plan(spec, llm, tools=(), feedback=""):
            seen_feedback.append(feedback)
            return (Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)

        def fail_fail_then_pass(
            piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None
        ):
            passed = iteration == 2  # third iteration is the one that finally passes
            return (step_result("t1", passed=passed, verdict="pass" if passed else "fail"),)

        monkeypatch.setattr("mace.orchestrator.plan", recording_plan)
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", fail_fail_then_pass)
        monkeypatch.setattr("mace.orchestrator.triage", lambda result, llm, tools=(): next(diagnoses))
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)

        run_mace_loop(
            ("/fake/root",), make_spec(budget=Budget(max_iterations=3)), FakeLLM(responses=[]),
            db=make_db(tmp_path),
        )

        assert len(seen_feedback) == 3
        assert "rtl_suspect" in seen_feedback[2] and "try a different mesh" in seen_feedback[2]
        assert "build_timeout" in seen_feedback[2] and "raise the sim wall clock" in seen_feedback[2]


class TestTriageToolServerConstructionFailure:
    def test_a_construction_failure_does_not_abort_the_run(self, tmp_path, monkeypatch):
        """PitonToolServer's own construction (e.g. ChiaTool's port search
        exhausted on a crowded worker) is a best-effort diagnostic aid --
        see _start_triage_tool_server's own docstring. A failure there must
        leave the failure triaged without it and let the run proceed, not
        propagate and abort a run that would otherwise have passed.
        """
        db = make_db(tmp_path)
        monkeypatch.setattr("mace.orchestrator.ray.is_initialized", lambda: True)

        def raising_tool_server(*args, **kwargs):
            raise RuntimeError("no free port")

        monkeypatch.setattr("mace.orchestrator.PitonToolServer", raising_tool_server)
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)] * 2),
        )
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            fake_integrate_parallel([step_result("t1", passed=False, verdict="fail"), step_result("t1")]),
        )
        triage_tools = []

        def recording_triage(result, llm, tools=()):
            triage_tools.append(tools)
            return Triage(diagnosis="rtl_suspect", fix="try a different mesh")

        monkeypatch.setattr("mace.orchestrator.triage", recording_triage)

        result = run_mace_loop(
            ("/fake/root",), make_spec(budget=Budget(max_iterations=2)), FakeLLM(responses=[]), db
        )

        assert result.status == "passed"
        assert triage_tools == [()]  # still triaged, just without the tool


class StubToolServers:
    """Stands in for the Ray actors ChiaTool.__post_init__ starts: records,
    per tool, the snapshot it would ship there, and which tools stopped."""

    def __init__(self):
        self.served: dict[PitonToolServer, PitonToolServer] = {}
        self.stopped: list[PitonToolServer] = []

    def start(self, tool):
        self.served[tool] = cloudpickle.loads(cloudpickle.dumps(tool))

    def stop(self, tool):
        self.stopped.append(tool)

    def grep_sim_log(self, tools):
        """The one PitonToolServer in *tools*, and what its served copy's
        grep says about its run's sim.log -- what a model's MCP call gets."""
        (tool,) = [t for t in tools if isinstance(t, PitonToolServer)]
        return tool, self.served[tool].grep("sim_log", "Simulation")


class TestTriageToolServerSeesTheFailure:
    """The triage tool server answers MCP calls from the snapshot of the tool
    ChiaTool.__post_init__ ships to its Ray actor, never from the object
    run_mace_loop holds -- so these query that snapshot, taken the same way
    (a cloudpickle round-trip), with no Ray instance."""

    @pytest.fixture
    def servers(self, monkeypatch):
        servers = StubToolServers()
        monkeypatch.setattr("mace.orchestrator.ray.is_initialized", lambda: True)
        monkeypatch.setattr(ChiaTool, "__post_init__", lambda tool: servers.start(tool))
        monkeypatch.setattr(ChiaTool, "stop", lambda tool: servers.stop(tool))
        return servers

    def test_triage_reaches_the_failed_run(self, tmp_path, stub_piton_root, monkeypatch, servers):
        """The regression: the failed run used to reach only run_mace_loop's
        own copy of the tool, so every grep a model made answered "no run
        yet"."""
        failed = failed_step_result(tmp_path / "run0", "1234 : Simulation -> FAIL(HIT BAD TRAP)\n")
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)]),
        )
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", fake_integrate_parallel([failed]))
        seen = []

        def grepping_triage(result, llm, tools=()):
            seen.append(servers.grep_sim_log(tools)[1])
            return Triage(diagnosis="test_bug", fix="fix the diag")

        monkeypatch.setattr("mace.orchestrator.triage", grepping_triage)
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)

        run_mace_loop((str(stub_piton_root),), make_spec(), FakeLLM(responses=[]), make_db(tmp_path))

        assert seen == ["1234 : Simulation -> FAIL(HIT BAD TRAP)"]

    def test_each_triage_gets_a_fresh_server_and_the_previous_one_is_stopped_first(
        self, tmp_path, stub_piton_root, monkeypatch, servers
    ):
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)] * 2),
        )
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            fake_integrate_parallel([
                failed_step_result(tmp_path / "run0", "1234 : Simulation -> FAIL(HIT BAD TRAP)\n"),
                failed_step_result(tmp_path / "run1", "5678 : Simulation -> FAIL(TIMEOUT)\n"),
            ]),
        )
        seen = []

        def grepping_triage(result, llm, tools=()):
            tool, out = servers.grep_sim_log(tools)
            seen.append((tool, out, list(servers.stopped)))
            return Triage(diagnosis="rtl_suspect", fix="try again")

        monkeypatch.setattr("mace.orchestrator.triage", grepping_triage)
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)

        run_mace_loop(
            (str(stub_piton_root),), make_spec(budget=Budget(max_iterations=2)), FakeLLM(responses=[]),
            make_db(tmp_path),
        )

        (first, first_out, stopped_by_first), (second, second_out, stopped_by_second) = seen
        assert first_out == "1234 : Simulation -> FAIL(HIT BAD TRAP)"
        assert second_out == "5678 : Simulation -> FAIL(TIMEOUT)"
        assert stopped_by_first == [] and stopped_by_second == [first]  # never two at once
        assert servers.stopped == [first, second]  # and the last one once the run ends

    def test_the_post_mortem_reaches_the_run_s_last_failure(
        self, tmp_path, stub_piton_root, monkeypatch, servers
    ):
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)] * 2),
        )
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            fake_integrate_parallel([
                failed_step_result(tmp_path / "run0", "1234 : Simulation -> FAIL(HIT BAD TRAP)\n"),
                failed_step_result(tmp_path / "run1", "5678 : Simulation -> FAIL(TIMEOUT)\n"),
            ]),
        )
        monkeypatch.setattr(
            "mace.orchestrator.triage",
            lambda result, llm, tools=(): Triage(diagnosis="rtl_suspect", fix="try again"),
        )
        seen = []

        def grepping_post_mortem(spec, iterations, diagnoses, status, llm, tools=()):
            tool, out = servers.grep_sim_log(tools)
            seen.append((out, tool in servers.stopped))
            raise ReportError("stubbed: only the tool it was given matters here")

        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", grepping_post_mortem)

        run_mace_loop(
            (str(stub_piton_root),), make_spec(budget=Budget(max_iterations=2)), FakeLLM(responses=[]),
            make_db(tmp_path),
        )

        assert seen == [("5678 : Simulation -> FAIL(TIMEOUT)", False)]  # still up at that point

    def test_a_run_that_passes_never_starts_one(self, tmp_path, stub_piton_root, monkeypatch, servers):
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)]),
        )
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", fake_integrate_parallel([step_result("t1")]))

        result = run_mace_loop((str(stub_piton_root),), make_spec(), FakeLLM(responses=[]), make_db(tmp_path))

        assert result.status == "passed"
        assert servers.served == {}
