"""Tier-0 tests for mace.eval.runner.

Run:
    pytest mace/test/test_eval_runner.py -q

The methods themselves are replaced with recording fakes; these tests
check job planning, resuming, cache clearing, dispatch, and the A3
re-simulation.
"""

from __future__ import annotations

import pytest

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics
from mace.eval import runner
from mace.eval.suite import SuiteTask
from mace.spec import Budget, LoopOptions, LoopResult, MaceSpec, StepResult, Task
from mace.test.conftest import FakeLLM


def suite_task(task_id="ariane-2x2-barrier", **override):
    kwargs = dict(
        id=task_id, core="ariane", mesh=(2, 2), workloads=("barrier_atomic.c",),
        objective="Verify it.", budget=Budget(max_iterations=3), verified=True,
    )
    kwargs.update(override)
    return SuiteTask(**kwargs)


def make_db(tmp_path):
    return metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)


def make_env(tmp_path, roots=("/a", "/b")):
    return runner.RunEnv(piton_roots=roots, llm=FakeLLM([]), db=make_db(tmp_path), meta={"model": "m"}, clear_cache=False)


class TestPlanJobs:
    def test_every_task_method_and_repeat(self):
        tasks = (suite_task("t1"), suite_task("t2"))
        jobs = runner.plan_jobs(tasks, ("mace", "one_shot", "expert"), repeats=3)
        keys = {j.key for j in jobs}
        assert len(jobs) == len(keys) == 2 * (3 + 3 + 1)
        assert ("t1", "expert", 0) in keys and ("t1", "expert", 1) not in keys

    def test_the_shuffle_depends_only_on_the_seed(self):
        tasks = tuple(suite_task(f"t{i}") for i in range(4))
        first = [j.key for j in runner.plan_jobs(tasks, ("mace", "one_shot"), 3, seed=7)]
        again = [j.key for j in runner.plan_jobs(tasks, ("mace", "one_shot"), 3, seed=7)]
        other = [j.key for j in runner.plan_jobs(tasks, ("mace", "one_shot"), 3, seed=8)]
        assert first == again
        assert first != other

    def test_expert_repeats_can_be_raised(self):
        jobs = runner.plan_jobs((suite_task(),), ("expert",), repeats=3, once_repeats=3)
        assert len(jobs) == 3

    def test_an_unknown_method_raises(self):
        with pytest.raises(ValueError, match="unknown methods"):
            runner.plan_jobs((suite_task(),), ("mace", "oracle"), 1)


class TestFinishedKeys:
    def test_only_finished_runs_count(self, tmp_path):
        db = make_db(tmp_path)
        spec = suite_task().spec()
        for status, repeat in (("passed", 0), ("budget_exceeded", 1), ("error", 2), ("running", 3)):
            run_id = metrics.start_run(db, spec, labels=metrics.RunLabels(method="mace", task="t", repeat=repeat))
            if status != "running":
                metrics.finish_run(db, run_id, status)
        metrics.start_run(db, spec)  # a plain run, not part of any batch
        assert runner.finished_keys(db) == {("t", "mace", 0), ("t", "mace", 1)}


class TestClearBuildCache:
    def test_removes_only_mace_model_directories(self, tmp_path):
        build = tmp_path / "build" / "manycore"
        for name in ("mace_abc123", "mace_def456", "hand_built", "rel-0.1"):
            (build / name / "obj_dir").mkdir(parents=True)
        (build / "mace_notadir").write_text("x")
        assert runner.clear_build_cache(str(tmp_path)) == 2
        assert sorted(p.name for p in build.iterdir()) == ["hand_built", "mace_notadir", "rel-0.1"]

    def test_a_checkout_without_builds_is_fine(self, tmp_path):
        assert runner.clear_build_cache(str(tmp_path)) == 0


class TestRunJob:
    @pytest.fixture
    def calls(self, monkeypatch):
        calls = []

        def recorder(name):
            def fake(*args, **kwargs):
                calls.append((name, args, kwargs))
                return LoopResult(run_id="r", status="budget_exceeded", iterations=())
            return fake

        for name in ("run_mace_loop", "run_one_shot", "run_retry_agent", "run_expert"):
            monkeypatch.setattr(f"mace.eval.runner.{name}", recorder(name))
        return calls

    def _run(self, tmp_path, method, env=None):
        env = env or make_env(tmp_path)
        runner.run_job(runner.Job(suite_task(), method, 1), env)
        return env

    @pytest.mark.parametrize(
        "method,function,roots,options",
        [
            ("mace", "run_mace_loop", ("/a", "/b"), LoopOptions()),
            ("one_checkout", "run_mace_loop", ("/a",), LoopOptions()),
            ("no_triage", "run_mace_loop", ("/a", "/b"), LoopOptions(triage="raw")),
            ("no_reuse", "run_mace_loop", ("/a", "/b"), LoopOptions(reuse_builds=False)),
            ("mace_rtl", "run_mace_loop", ("/a", "/b"), LoopOptions(rtl_edits=True)),
            ("build_check", "run_mace_loop", ("/a", "/b"), LoopOptions(check="build")),
        ],
    )
    def test_loop_methods(self, tmp_path, calls, method, function, roots, options):
        self._run(tmp_path, method)
        ((name, args, kwargs),) = calls
        assert name == function
        assert args[0] == roots
        assert kwargs["options"] == options
        labels = kwargs["labels"]
        assert (labels.method, labels.task, labels.repeat) == (method, "ariane-2x2-barrier", 1)
        assert labels.meta["piton_roots"] == list(roots)
        assert labels.meta["model"] == "m"

    def test_one_shot_and_the_baselines(self, tmp_path, calls):
        for method in ("one_shot", "retry_agent", "expert"):
            self._run(tmp_path, method)
        (one_shot, retry, expert) = calls
        assert one_shot[0] == "run_one_shot" and one_shot[1][0] == ("/a", "/b")
        assert retry[0] == "run_retry_agent" and retry[1][0] == "/a"
        assert expert[0] == "run_expert" and expert[1][0] == ("/a",)
        assert expert[1][2].id == "expert"

    def test_the_rtl_retry_agent_edits_rtl_on_one_checkout(self, tmp_path, calls):
        self._run(tmp_path, "retry_agent_rtl")
        ((name, args, kwargs),) = calls
        assert name == "run_retry_agent" and args[0] == "/a" and kwargs["rtl_edits"] is True

    def test_a_task_with_a_source_fault_runs_every_checkout_under_it(self, tmp_path, calls, monkeypatch):
        from mace.eval.source_faults import SourceEdit

        seen = []

        class Held:
            def __enter__(self):
                seen.append("applied")

            def __exit__(self, *exc):
                seen.append("restored")

        monkeypatch.setattr(runner, "faulted", lambda roots, edits: (seen.append((roots, edits)), Held())[1])
        fault = (SourceEdit("piton/design/x.v", "a", "b"),)
        env = make_env(tmp_path)

        runner.run_job(runner.Job(suite_task(source_fault=fault), "mace", 0), env)

        assert seen[0] == (("/a", "/b"), fault)
        assert seen[1:] == ["applied", "restored"]
        assert calls[0][2]["labels"].meta["source_fault"] == ["piton/design/x.v"]

    def test_a_task_without_a_fault_applies_nothing(self, tmp_path, calls, monkeypatch):
        monkeypatch.setattr(runner, "faulted", lambda *a: pytest.fail("no fault to apply"))
        self._run(tmp_path, "mace")
        assert "source_fault" not in calls[0][2]["labels"].meta

    def test_task_prompts_off_reaches_the_loop(self, tmp_path, calls):
        env = make_env(tmp_path)
        env.task_prompts = False
        self._run(tmp_path, "mace", env)
        assert calls[0][2]["options"].task_prompts is False
        assert calls[0][2]["labels"].meta["task_prompts"] is False

    def test_the_build_cache_is_emptied_first(self, tmp_path, calls):
        (tmp_path / "build" / "manycore" / "mace_1").mkdir(parents=True)
        env = make_env(tmp_path, roots=(str(tmp_path),))
        env.clear_cache = True
        self._run(tmp_path, "mace", env)
        assert not (tmp_path / "build" / "manycore" / "mace_1").exists()


class TestResimulateAccepted:
    def _accepted(self, root, task_id="t1"):
        build = PitonBuildArtifact(
            success=True, returncode=0, config=PitonConfig(x_tiles=2, y_tiles=2), sim_type="vlt",
            model_dir=f"{root}/build/manycore/mace_abc", binary_path="x", wall_time_s=1.0,
        )
        task = Task(id=task_id, deps=(), kind="config", spec="s")
        query = FakeLLM(responses=[""]).prompt("")
        return StepResult(task=task, query=query, build=build, run=None, passed=True)

    def test_simulates_each_accepted_design_on_its_checkout(self, tmp_path):
        db = make_db(tmp_path)
        spec = MaceSpec(workloads=("a.c", "b.c"), objective="o")
        run_id = metrics.start_run(db, spec)
        result = LoopResult(run_id=run_id, status="passed", iterations=((self._accepted("/ckout/one"),),))
        seen = []

        def run_program(root, config, program, spec):
            seen.append((root, program))
            return PitonRunResult(
                success=program == "a.c", returncode=0, test=program, sim_type="vlt", run_dir="/x",
                verdict="pass" if program == "a.c" else "timeout",
            )

        assert runner.resimulate_accepted(db, result, spec, run_program) == 1
        assert seen == [("/ckout/one", "a.c"), ("/ckout/one", "b.c")]
        rows = db.query("SELECT program, verdict, passed FROM resimulations WHERE run_id = ? ORDER BY program", (run_id,))
        assert [(r["program"], r["verdict"], r["passed"]) for r in rows] == [("a.c", "pass", 1), ("b.c", "timeout", 0)]

    def test_a_run_that_did_not_pass_has_nothing_to_check(self, tmp_path):
        result = LoopResult(run_id="r", status="budget_exceeded", iterations=((self._accepted("/c"),),))
        assert runner.resimulate_accepted(make_db(tmp_path), result, MaceSpec(workloads=("a.c",), objective="o"), None) == 0

    def test_a_resimulation_runs_with_the_spec_s_limits(self, monkeypatch):
        seen = {}

        class _Node:
            @staticmethod
            def run(root, config, program, **kwargs):
                seen.update(kwargs)

        monkeypatch.setattr(runner, "OpenPitonWorkspaceNode", _Node)
        spec = MaceSpec(workloads=("a.c",), objective="o", rtl_timeout=4_000_000, max_cycle=6_000_000)
        runner._run_program("/c", PitonConfig(), "a.c", spec)
        assert (seen["rtl_timeout"], seen["max_cycle"]) == (4_000_000, 6_000_000)


class TestRunBatch:
    def test_skips_finished_jobs_and_survives_a_failing_one(self, tmp_path, monkeypatch):
        env = make_env(tmp_path)
        spec = suite_task().spec()
        done = metrics.start_run(env.db, spec, labels=metrics.RunLabels(method="mace", task="ariane-2x2-barrier", repeat=0))
        metrics.finish_run(env.db, done, "passed")
        ran = []

        def fake_run_job(job, env):
            ran.append(job.key)
            if job.method == "one_shot":
                raise RuntimeError("boom")
            return LoopResult(run_id="r", status="passed", iterations=())

        monkeypatch.setattr("mace.eval.runner.run_job", fake_run_job)
        reported = []
        jobs = runner.plan_jobs((suite_task(),), ("mace", "one_shot"), repeats=2)
        counts = runner.run_batch(jobs, env, on_job=lambda job, outcome: reported.append((job.key, outcome)))

        assert counts == {"skipped": 1, "ran": 1, "errors": 2}
        assert ("ariane-2x2-barrier", "mace", 0) not in ran
        assert len(reported) == 3
        assert any(isinstance(outcome, RuntimeError) for _, outcome in reported)


class TestEnvironmentMeta:
    def test_records_the_probes_and_survives_a_failing_one(self, monkeypatch):
        monkeypatch.setattr(
            runner.OpenPitonWorkspaceNode, "verilator_version_text",
            staticmethod(lambda root: "Verilator 5.052 2026-09-05\nmore"),
        )

        def failing_fingerprint(root, version):
            raise RuntimeError("no git")

        monkeypatch.setattr(runner.OpenPitonWorkspaceNode, "source_fingerprint", staticmethod(failing_fingerprint))
        meta = runner.environment_meta(("/ck",), "vertex", "gemini-2.5-flash")
        assert (meta["backend"], meta["model"]) == ("vertex", "gemini-2.5-flash")
        assert meta["checkouts"]["/ck"]["verilator"] == "Verilator 5.052 2026-09-05"
        assert meta["checkouts"]["/ck"]["fingerprint"].startswith("error: ")
        assert "mace_commit" in meta and "host" in meta


def codesign_task(task_id="cd-task"):
    from mace.codesign.space import DesignSpace
    from mace.eval.suite import CodesignConfig

    space = DesignSpace(sizes={"l1d": (4096, 8192)}, assocs={"l1d": (2, 4)}, networks=("2dmesh_config", "xbar_config"))
    config = CodesignConfig(space=space, grid={"l1d_size": (4096, 8192)}, simulations=6, batch=2, area_budget_ratio=1.0)
    return suite_task(task_id, workloads=("matmul.c",), codesign=config)


class TestCodesignJobs:
    def test_methods_run_only_on_the_tasks_they_apply_to(self):
        jobs = runner.plan_jobs((suite_task(), codesign_task()), ("mace", "codesign_random", "codesign_grid"), repeats=2)
        keys = {j.key for j in jobs}
        assert keys == {
            ("ariane-2x2-barrier", "mace", 0), ("ariane-2x2-barrier", "mace", 1),
            ("cd-task", "codesign_random", 0), ("cd-task", "codesign_random", 1),
            ("cd-task", "codesign_grid", 0),
        }

    @pytest.mark.parametrize(
        "method,strategy,seed",
        [
            ("codesign_mace", "LLMProposer", None),
            ("codesign_random", "RandomSearch", 1),
            ("codesign_grid", "GridSearch", None),
        ],
    )
    def test_each_method_searches_with_its_strategy(self, tmp_path, monkeypatch, method, strategy, seed):
        calls = []
        monkeypatch.setattr(
            "mace.eval.runner.run_codesign",
            lambda roots, spec, strat, db, **kwargs: calls.append((roots, strat, kwargs)) or "done",
        )
        assert runner.run_job(runner.Job(codesign_task(), method, 1), make_env(tmp_path)) == "done"
        ((roots, strat, kwargs),) = calls
        assert type(strat).__name__ == strategy
        assert roots == ("/a", "/b")
        assert (kwargs["simulations"], kwargs["batch"]) == (6, 2)
        assert kwargs["area_budget_um2"] > 0
        assert kwargs["labels"].seed == seed
        assert kwargs["labels"].meta["area_budget_um2"] == kwargs["area_budget_um2"]

    def test_the_area_budget_is_the_ratio_times_the_default_caches(self):
        from mace.codesign.area import design_area

        task = codesign_task()
        expected = design_area(task.codesign.default_design(), 4).area_um2
        assert runner.area_budget_um2(task) == pytest.approx(expected)


class TestNoLLMMethods:
    def test_each_runs_with_no_llm(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mace.eval.runner.run_codesign", lambda roots, spec, strat, db, **kwargs: "done")
        monkeypatch.setattr("mace.eval.runner.run_expert", lambda roots, spec, task, db, **kwargs: "done")
        env = runner.RunEnv(piton_roots=("/a",), llm=None, db=make_db(tmp_path), clear_cache=False)
        for method in sorted(runner.NO_LLM_METHODS):
            task = codesign_task() if method in runner.CODESIGN_METHODS else suite_task()
            assert runner.run_job(runner.Job(task, method, 0), env) == "done"

    def test_the_llm_proposer_is_not_among_them(self):
        assert runner.NO_LLM_METHODS < set(runner.METHODS)
        assert "codesign_mace" not in runner.NO_LLM_METHODS


class TestSeededJobs:
    def test_a_fault_runs_only_on_its_cores(self):
        pico = suite_task("pico-task", core="pico")
        jobs = runner.plan_jobs((suite_task(), pico, codesign_task()), ("seeded_drop_bist", "seeded_fpga_synth"), repeats=1)
        assert {j.key for j in jobs} == {
            ("pico-task", "seeded_drop_bist", 0),
            ("pico-task", "seeded_fpga_synth", 0),
            ("ariane-2x2-barrier", "seeded_fpga_synth", 0),
        }

    def test_the_crossbar_fault_skips_a_mesh_with_one_row(self):
        row = suite_task("ariane-4x1-row", mesh=(4, 1))
        jobs = runner.plan_jobs((suite_task(), row), ("seeded_crossbar", "faultcheck_crossbar"), repeats=2)
        assert {j.key for j in jobs} == {
            ("ariane-2x2-barrier", "seeded_crossbar", 0), ("ariane-2x2-barrier", "seeded_crossbar", 1),
            ("ariane-2x2-barrier", "faultcheck_crossbar", 0),
        }

    def test_a_fault_check_builds_the_expert_configuration_with_the_fault(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "mace.eval.runner.run_expert",
            lambda roots, spec, task, db, **kwargs: calls.append((roots, task, kwargs)) or "done",
        )
        env = runner.RunEnv(piton_roots=("/a", "/b"), llm=None, db=make_db(tmp_path), clear_cache=False)
        assert runner.run_job(runner.Job(suite_task(), "faultcheck_crossbar", 0), env) == "done"
        ((roots, task, kwargs),) = calls
        assert roots == ("/a",)
        assert (task.id, task.network) == ("expert", "xbar_config")
        assert kwargs["labels"].meta["fault"] == "crossbar"

    def test_fault_checks_run_once_and_call_no_llm(self):
        for name in ("fpga_synth", "drop_bist", "crossbar", "l1d_eight_way"):
            assert f"faultcheck_{name}" in runner.ONCE_METHODS
            assert f"faultcheck_{name}" in runner.NO_LLM_METHODS

    def test_the_loop_runs_with_the_fault_s_first_plan_breaker(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "mace.eval.runner.run_mace_loop",
            lambda roots, spec, llm, db, **kwargs: calls.append(kwargs) or "done",
        )
        runner.run_job(runner.Job(suite_task(), "seeded_fpga_synth", 0), make_env(tmp_path))
        (kwargs,) = calls
        assert kwargs["labels"].meta["fault"] == "fpga_synth"
        hooked = kwargs["plan_hook"](0, (Task(id="t", deps=(), kind="config", spec="s"),))
        assert hooked[0].config_rtl == ("PITON_FPGA_SYNTH",)
