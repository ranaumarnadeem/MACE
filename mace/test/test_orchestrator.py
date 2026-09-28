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
        nodes=None, **kwargs,
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
            nodes=None, **kwargs,
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
            nodes=None, **kwargs,
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
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None, **kwargs: (
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
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None, **kwargs: (
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
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None, **kwargs: (
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
            lambda piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None, **kwargs: (
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
            piton_roots, spec, tasks, llm, tools=(), run_id=None, iteration=0, on_task_progress=None, nodes=None, **kwargs
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


class TestEveryExceptionRecordsError:
    """Every exception that ends a run, including those raised outside the
    iteration loop itself, must record the run as "error" and propagate, so
    a crashed run never stays "running" in the database."""

    @staticmethod
    def recorded_status(db):
        return db.query("SELECT status FROM runs")[0]["status"]

    def test_a_checksum_mismatch_still_records_checksum_mismatch(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)

        def mismatching_checksums():
            raise ValueError("barrier_atomic.c does not match CHECKSUMS")

        monkeypatch.setattr("mace.orchestrator.verify_checksums", mismatching_checksums)

        result = run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db)

        assert result.status == "checksum_mismatch"
        assert self.recorded_status(db) == "checksum_mismatch"

    def test_a_checksum_check_crash_records_error(self, tmp_path, monkeypatch):
        """Only ValueError means a mismatch; a missing CHECKSUMS file is a crash."""
        db = make_db(tmp_path)

        def missing_checksums():
            raise FileNotFoundError("mace/workloads/CHECKSUMS")

        monkeypatch.setattr("mace.orchestrator.verify_checksums", missing_checksums)

        with pytest.raises(FileNotFoundError):
            run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db)

        assert self.recorded_status(db) == "error"

    def test_a_failure_to_stop_the_last_tool_server_records_error(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)

        class UnstoppableServer:
            def stop(self):
                raise RuntimeError("actor already gone")

        monkeypatch.setattr("mace.orchestrator.ray.is_initialized", lambda: True)
        monkeypatch.setattr(
            "mace.orchestrator._start_triage_tool_server",
            lambda run_id, piton_root, failed: UnstoppableServer(),
        )
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)]),
        )
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            fake_integrate_parallel([step_result("t1", passed=False, verdict="fail")]),
        )
        monkeypatch.setattr(
            "mace.orchestrator.triage",
            lambda result, llm, tools=(): Triage(diagnosis="rtl_suspect", fix="try again"),
        )
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)

        with pytest.raises(RuntimeError, match="actor already gone"):
            run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db)

        assert self.recorded_status(db) == "error"

    def test_a_failure_to_mark_recovery_records_error(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)

        def locked_mark_all_recovered(db, run_id):
            raise RuntimeError("database is locked")

        monkeypatch.setattr("mace.orchestrator.mark_all_recovered", locked_mark_all_recovered)
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)] * 2),
        )
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            fake_integrate_parallel([step_result("t1", passed=False, verdict="fail"), step_result("t1")]),
        )
        monkeypatch.setattr(
            "mace.orchestrator.triage",
            lambda result, llm, tools=(): Triage(diagnosis="rtl_suspect", fix="try again"),
        )

        with pytest.raises(RuntimeError, match="database is locked"):
            run_mace_loop(
                ("/fake/root",), make_spec(budget=Budget(max_iterations=2)), FakeLLM(responses=[]), db
            )

        assert self.recorded_status(db) == "error"

    def test_a_failed_final_status_write_records_error(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)

        def finish_run_failing_on_passed(db, run_id, status):
            if status == "passed":
                raise RuntimeError("disk I/O error")
            metrics.finish_run(db, run_id, status)

        monkeypatch.setattr("mace.orchestrator.finish_run", finish_run_failing_on_passed)
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            fake_plan([(Task(id="t1", deps=(), kind="workload", spec="hello_world.c"),)]),
        )
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", fake_integrate_parallel([step_result("t1")]))

        with pytest.raises(RuntimeError, match="disk I/O error"):
            run_mace_loop(("/fake/root",), make_spec(), FakeLLM(responses=[]), db)

        assert self.recorded_status(db) == "error"


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


def _costed(text, usd):
    from mace.llm import VertexQueryResult

    return VertexQueryResult(
        result=text, returncode=0, stderr="", stream_result=text, success=True,
        usage={"cost_usd": usd, "input_tokens": 100, "output_tokens": 10, "thinking_tokens": 50},
    )


class TestLlmCallsAreRecorded:
    PLAN = "TASK: t1 | deps= | kind=workload | run the gate workload"
    TRIAGE = "DIAGNOSIS: timeout\nFIX: raise the timeout"

    def test_plan_and_triage_calls_land_in_their_own_iteration(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        llm = FakeLLM(responses=[_costed(self.PLAN, 0.1), _costed(self.TRIAGE, 0.2), _costed(self.PLAN, 0.4)])
        outcomes = iter([[step_result("t1", passed=False, verdict="timeout")], [step_result("t1")]])
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", lambda *a, **k: tuple(next(outcomes)))

        result = run_mace_loop(("/fake",), make_spec(budget=Budget(max_iterations=2)), llm, db)

        assert result.status == "passed"
        rows = db.query(
            "SELECT iteration, phase, usd, thinking_tokens FROM llm_calls WHERE run_id = ? ORDER BY seq",
            (result.run_id,),
        )
        assert [(r["iteration"], r["phase"]) for r in rows] == [(0, "plan"), (0, "triage"), (1, "plan")]
        assert rows[0]["thinking_tokens"] == 50
        usd = {
            r["iteration"]: r["usd"]
            for r in db.query("SELECT iteration, usd FROM iterations WHERE run_id = ?", (result.run_id,))
        }
        assert usd[0] == pytest.approx(0.3)
        assert usd[1] == pytest.approx(0.4)
        assert metrics.summary(db, result.run_id)["compute_usd"] == pytest.approx(0.7)

    def test_planner_and_triage_cost_count_toward_max_usd(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        llm = FakeLLM(responses=[_costed(self.PLAN, 3.0), _costed(self.TRIAGE, 2.0)])
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            lambda *a, **k: (step_result("t1", passed=False, verdict="timeout"),),
        )
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)

        result = run_mace_loop(
            ("/fake",), make_spec(budget=Budget(max_iterations=3, max_usd=4.0)), llm, db
        )

        assert result.status == "budget_exceeded"
        assert len(result.iterations) == 1

    def test_the_post_mortem_call_belongs_to_the_whole_run(self, tmp_path, monkeypatch):
        from mace import usage
        from mace.spec import PostMortem

        db = make_db(tmp_path)
        llm = FakeLLM(responses=[_costed(self.PLAN, 0.1), _costed(self.TRIAGE, 0.1)])
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            lambda *a, **k: (step_result("t1", passed=False, verdict="timeout"),),
        )

        def post_mortem(*args, **kwargs):
            usage.note("post_mortem", _costed("ASSESSMENT: config", 0.05), 1.0)
            return PostMortem(assessment="config")

        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", post_mortem)
        result = run_mace_loop(("/fake",), make_spec(), llm, db)

        last = db.query_one(
            "SELECT iteration, phase FROM llm_calls WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
            (result.run_id,),
        )
        assert (last["iteration"], last["phase"]) == (None, "post_mortem")

    def test_labels_reach_the_runs_table(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        monkeypatch.setattr(
            "mace.orchestrator.plan",
            lambda spec, llm, tools=(), feedback="": (Task(id="t1", deps=(), kind="workload", spec="w"),),
        )
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", lambda *a, **k: (step_result("t1"),))
        result = run_mace_loop(
            ("/fake",), make_spec(), FakeLLM(responses=[]), db,
            labels=metrics.RunLabels(method="one_shot", task="ariane-2x2", repeat=1),
        )
        row = db.query_one("SELECT method, task, repeat FROM runs WHERE run_id = ?", (result.run_id,))
        assert (row["method"], row["task"], row["repeat"]) == ("one_shot", "ariane-2x2", 1)


class TestLoopOptions:
    def _failing_then_recorded(self, monkeypatch, received):
        def integrate(piton_roots, spec, tasks, llm, **kwargs):
            received.append(kwargs)
            return (step_result("t1", passed=False, verdict="timeout"),)

        monkeypatch.setattr("mace.orchestrator.integrate_parallel", integrate)

    def test_options_and_a_deadline_reach_the_integrator(self, tmp_path, monkeypatch):
        from mace.spec import LoopOptions

        received = []
        self._failing_then_recorded(monkeypatch, received)
        monkeypatch.setattr("mace.orchestrator.plan", fake_plan([(Task(id="t1", deps=(), kind="workload", spec="w"),)]))
        monkeypatch.setattr("mace.orchestrator.triage", lambda result, llm, tools=(): Triage("timeout", "wait"))
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)
        opts = LoopOptions(check="build", reuse_builds=False)
        before = time.monotonic()
        run_mace_loop(("/fake",), make_spec(budget=Budget(max_iterations=1, max_wall_s=900)), FakeLLM([]), make_db(tmp_path), options=opts)

        assert received[0]["options"] is opts
        assert before + 890 <= received[0]["deadline"] <= time.monotonic() + 900

    def _replan_feedback(self, tmp_path, monkeypatch, options):
        feedback_seen = []

        def recording_plan(spec, llm, tools=(), feedback=""):
            feedback_seen.append(feedback)
            return (Task(id="t1", deps=(), kind="workload", spec="w"),)

        def no_triage(*args, **kwargs):
            raise AssertionError("triage's LLM call must not run in this mode")

        monkeypatch.setattr("mace.orchestrator.plan", recording_plan)
        monkeypatch.setattr("mace.orchestrator.triage", no_triage)
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", _no_post_mortem)
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            lambda *a, **k: (step_result("t1", passed=False, verdict="timeout"),),
        )
        db = make_db(tmp_path)
        result = run_mace_loop(("/fake",), make_spec(budget=Budget(max_iterations=2)), FakeLLM([]), db, options=options)
        diagnosis = db.query_value(
            "SELECT diagnosis FROM failures WHERE run_id = ? AND iteration = 0", (result.run_id,)
        )
        return feedback_seen, diagnosis

    def test_raw_triage_hands_the_evidence_to_the_next_plan(self, tmp_path, monkeypatch):
        from mace.spec import LoopOptions

        feedback, diagnosis = self._replan_feedback(tmp_path, monkeypatch, LoopOptions(triage="raw"))
        assert feedback[0] == ""
        assert "What the build and simulation reported" in feedback[1]
        assert "Build flags:" in feedback[1]
        assert "run verdict: timeout" in feedback[1]
        assert diagnosis == "raw_evidence"

    def test_triage_off_hands_the_next_plan_nothing(self, tmp_path, monkeypatch):
        from mace.spec import LoopOptions

        feedback, diagnosis = self._replan_feedback(tmp_path, monkeypatch, LoopOptions(triage="off"))
        assert feedback == ["", ""]
        assert diagnosis == "not_triaged"

    def test_post_mortem_off_skips_it(self, tmp_path, monkeypatch):
        from mace.spec import LoopOptions

        def no_post_mortem(*args, **kwargs):
            raise AssertionError("post-mortem is off")

        monkeypatch.setattr("mace.orchestrator.plan", fake_plan([(Task(id="t1", deps=(), kind="workload", spec="w"),)]))
        monkeypatch.setattr("mace.orchestrator.triage", lambda result, llm, tools=(): Triage("timeout", "wait"))
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", no_post_mortem)
        monkeypatch.setattr(
            "mace.orchestrator.integrate_parallel",
            lambda *a, **k: (step_result("t1", passed=False, verdict="timeout"),),
        )
        result = run_mace_loop(
            ("/fake",), make_spec(), FakeLLM([]), make_db(tmp_path), options=LoopOptions(post_mortem=False)
        )
        assert result.status == "budget_exceeded"
        assert result.post_mortem is None
