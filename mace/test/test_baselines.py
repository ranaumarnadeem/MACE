"""Tier-0 tests for mace.baselines.

Run:
    pytest mace/test/test_baselines.py -q

The build-and-check path (mace.integrator.integrate_parallel) is replaced
with a fake, as in test_orchestrator.py; these tests check what each
baseline asks, what it records, and when it stops.
"""

from __future__ import annotations

import pytest

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics, planner
from mace.baselines import expert, one_shot, retry_agent
from mace.llm import VertexQueryResult
from mace.spec import Budget, LoopOptions, MaceSpec, StepResult
from mace.test.conftest import FakeLLM


def make_spec(**override):
    kwargs = {
        "workloads": ("barrier_atomic.c",),
        "objective": "Verify the barrier_atomic.c gate workload passes on a 2x2 mesh.",
        "target_mesh": (2, 2),
        "budget": Budget(max_iterations=3),
    }
    kwargs.update(override)
    return MaceSpec(**kwargs)


def make_db(tmp_path):
    return metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)


def step(task, passed, verdict="pass"):
    build = PitonBuildArtifact(
        success=True, returncode=0, config=PitonConfig(), sim_type="vlt", model_dir="/x",
        binary_path="/x/V", wall_time_s=5.0,
    )
    run = PitonRunResult(
        success=passed, returncode=0, test="barrier_atomic.c", sim_type="vlt", run_dir="/x",
        verdict=verdict, sim_log_tail="SIM LOG TAIL", wall_time_s=2.0,
    )
    query = FakeLLM(responses=[""]).prompt("")
    return StepResult(task=task, query=query, build=build, run=run, passed=passed, runs=(run,))


DESIGN = "TASK: design | deps= | kind=config | defaults with a larger L2\nCACHES: design | l2=131072,4"


class TestSharedInputs:
    def test_the_retry_agent_states_the_inputs_as_the_planner_does(self):
        spec = make_spec()
        inputs = planner.render_inputs(spec)
        assert inputs in planner.build_prompt(spec)
        assert inputs in retry_agent.build_prompt(spec, [])
        assert planner.OVERRIDE_RULES in planner.build_prompt(spec)
        assert planner.OVERRIDE_RULES in retry_agent.build_prompt(spec, [])

    def test_history_is_appended_oldest_first(self):
        prompt = retry_agent.build_prompt(make_spec(), ["first failure", "second failure"])
        assert prompt.index("first failure") < prompt.index("second failure")
        assert "Earlier attempts, oldest first" in prompt


class TestParseDesign:
    def test_reads_the_design_task_and_its_overrides(self):
        task = retry_agent.parse_design(DESIGN)
        assert task.id == "design"
        assert task.caches_dict == {"l2": (131072, 4)}

    def test_falls_back_to_the_first_config_task(self):
        task = retry_agent.parse_design("TASK: t9 | deps=t1 | kind=config | try it")
        assert (task.id, task.deps) == ("t9", ())

    def test_none_without_a_task_line(self):
        assert retry_agent.parse_design("I would enlarge the L2.") is None

    def test_unit_test_tasks_are_not_designs(self):
        assert retry_agent.parse_design("TASK: u | deps= | kind=unit_test | piton/x.v") is None


class TestRetryAgentRun:
    @pytest.fixture(autouse=True)
    def _no_nodes(self, monkeypatch):
        monkeypatch.setattr("mace.baselines.retry_agent.open_nodes", lambda roots: [])
        monkeypatch.setattr("mace.baselines.retry_agent.close_nodes", lambda nodes: None)

    def _integrate(self, monkeypatch, outcomes, received):
        outcomes = iter(outcomes)

        def fake(piton_roots, spec, tasks, llm, **kwargs):
            received.append((piton_roots, tasks, kwargs))
            passed, verdict = next(outcomes)
            return (step(tasks[0], passed, verdict),)

        monkeypatch.setattr("mace.baselines.retry_agent.integrate_parallel", fake)

    def test_a_failure_s_evidence_reaches_the_next_attempt(self, tmp_path, monkeypatch):
        received = []
        self._integrate(monkeypatch, [(False, "timeout"), (True, "pass")], received)
        llm = FakeLLM(responses=[DESIGN, DESIGN])
        db = make_db(tmp_path)

        result = retry_agent.run_retry_agent("/root", make_spec(), llm, db)

        assert result.status == "passed"
        assert len(result.iterations) == 2
        second_prompt = llm.calls[1][0]
        assert "Attempt 1 failed (build succeeded: True, run verdict: timeout)" in second_prompt
        assert "SIM LOG TAIL" in second_prompt
        roots, tasks, kwargs = received[0]
        assert roots == ("/root",)
        assert kwargs["options"] == LoopOptions(task_prompts=False)
        assert kwargs["deadline"] is not None
        failure = db.query_one("SELECT diagnosis, recovered FROM failures WHERE run_id = ?", (result.run_id,))
        assert (failure["diagnosis"], failure["recovered"]) == ("raw_evidence", 1)

    def test_runs_are_labelled_and_calls_recorded(self, tmp_path, monkeypatch):
        self._integrate(monkeypatch, [(True, "pass")], [])
        reply = VertexQueryResult(
            result=DESIGN, returncode=0, stderr="", stream_result="", success=True,
            usage={"cost_usd": 0.02, "input_tokens": 50},
        )
        db = make_db(tmp_path)
        result = retry_agent.run_retry_agent(
            "/root", make_spec(), FakeLLM(responses=[reply]), db,
            labels=metrics.RunLabels(method="retry_agent", task="t", repeat=0),
        )
        row = db.query_one("SELECT method, task, status FROM runs WHERE run_id = ?", (result.run_id,))
        assert (row["method"], row["task"], row["status"]) == ("retry_agent", "t", "passed")
        call = db.query_one("SELECT phase, usd FROM llm_calls WHERE run_id = ?", (result.run_id,))
        assert (call["phase"], call["usd"]) == ("agent", 0.02)

    def test_a_reply_without_a_design_uses_up_its_attempt(self, tmp_path, monkeypatch):
        received = []
        self._integrate(monkeypatch, [(True, "pass")], received)
        llm = FakeLLM(responses=["no idea", DESIGN])

        result = retry_agent.run_retry_agent("/root", make_spec(), llm, make_db(tmp_path))

        assert result.status == "passed"
        assert result.iterations[0] == ()
        assert "Attempt 1 named no design" in llm.calls[1][0]
        assert len(received) == 1

    def test_stops_after_the_attempt_budget(self, tmp_path, monkeypatch):
        self._integrate(monkeypatch, [(False, "fail")] * 2, [])
        result = retry_agent.run_retry_agent(
            "/root", make_spec(budget=Budget(max_iterations=2)), FakeLLM(responses=[DESIGN, DESIGN]),
            make_db(tmp_path),
        )
        assert result.status == "budget_exceeded"
        assert len(result.iterations) == 2

    def test_a_changed_gate_program_stops_before_any_attempt(self, tmp_path, monkeypatch):
        def mismatch():
            raise ValueError("checksum mismatch for: ['barrier_atomic.c']")

        monkeypatch.setattr("mace.baselines.retry_agent.verify_checksums", mismatch)
        llm = FakeLLM(responses=[])
        result = retry_agent.run_retry_agent("/root", make_spec(), llm, make_db(tmp_path))
        assert result.status == "checksum_mismatch"
        assert llm.calls == []


class TestExpert:
    def test_builds_the_expert_configuration_without_an_llm(self, tmp_path, monkeypatch):
        received = []

        def fake(piton_roots, spec, tasks, llm, **kwargs):
            received.append((piton_roots, tasks, llm, kwargs))
            return (step(tasks[0], True),)

        monkeypatch.setattr("mace.baselines.expert.integrate_parallel", fake)
        task = expert.expert_task(caches={"l2": (65536, 4)}, config_rtl=("CONFIG_DISABLE_BIST_CLEAR",))
        db = make_db(tmp_path)

        result = expert.run_expert(("/a", "/b"), make_spec(), task, db)

        assert result.status == "passed"
        roots, tasks, llm, kwargs = received[0]
        assert roots == ("/a",)
        assert llm is None
        assert kwargs["options"] == LoopOptions(task_prompts=False)
        assert tasks[0].config_rtl == ("CONFIG_DISABLE_BIST_CLEAR",)
        assert tasks[0].caches_dict == {"l2": (65536, 4)}
        assert db.query_value("SELECT method FROM runs WHERE run_id = ?", (result.run_id,)) == "expert"

    def test_a_failing_expert_configuration_ends_budget_exceeded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mace.baselines.expert.integrate_parallel",
            lambda piton_roots, spec, tasks, llm, **kwargs: (step(tasks[0], False, "fail"),),
        )
        result = expert.run_expert(("/a",), make_spec(), expert.expert_task(), make_db(tmp_path))
        assert result.status == "budget_exceeded"

    def test_an_expert_task_without_overrides_keeps_the_defaults(self):
        task = expert.expert_task()
        assert (task.caches, task.config_rtl, task.kind) == (None, None, "config")


class TestOneShot:
    def test_is_the_loop_with_one_iteration_and_no_triage(self, tmp_path, monkeypatch):
        received = {}

        def fake_loop(piton_roots, spec, llm, db, **kwargs):
            received.update(spec=spec, **kwargs)
            return "result"

        monkeypatch.setattr("mace.baselines.one_shot.run_mace_loop", fake_loop)
        assert one_shot.run_one_shot(("/a", "/b"), make_spec(), FakeLLM([]), make_db(tmp_path)) == "result"
        assert received["spec"].budget.max_iterations == 1
        assert received["spec"].budget.max_wall_s == make_spec().budget.max_wall_s
        assert received["options"] == LoopOptions(triage="off", post_mortem=False)
        assert received["labels"].method == "one_shot"

    def test_task_prompts_can_be_turned_off(self, tmp_path, monkeypatch):
        received = {}
        monkeypatch.setattr(
            "mace.baselines.one_shot.run_mace_loop",
            lambda piton_roots, spec, llm, db, **kwargs: received.update(kwargs),
        )
        one_shot.run_one_shot(("/a",), make_spec(), FakeLLM([]), make_db(tmp_path), task_prompts=False)
        assert received["options"].task_prompts is False
