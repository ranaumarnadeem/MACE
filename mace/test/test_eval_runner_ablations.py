"""Tier-0 tests for session A's ablation methods in mace.eval.runner.

Run:
    pytest mace/test/test_eval_runner_ablations.py -q
"""

from __future__ import annotations

import pytest

from mace.eval import runner
from mace.eval.faults import FAULTS
from mace.spec import LoopOptions, Task
from mace.test.test_eval_runner import make_env, suite_task

PARTS = ("plan_check", "raw_evidence", "narrowing", "fast_path", "gate_pass_ends_run")


class TestRegistered:
    def test_each_part_has_a_method_with_it_off(self):
        for part in PARTS:
            assert f"no_{part}" in runner.METHODS

    def test_each_part_and_fault_has_a_seeded_ablation(self):
        for part in PARTS:
            for name in FAULTS:
                assert f"seeded_no_{part}_{name}" in runner.METHODS
        seeded = {m for m in runner.METHODS if (runner.a_ablation(m) or (None, None))[1] is not None}
        assert len(seeded) == len(PARTS) * len(FAULTS)

    def test_a_fault_named_no_mem_ctrl_is_no_ablation(self):
        # seeded_no_mem_ctrl starts with seeded_no_ but is the seeded run of the no_mem_ctrl fault.
        assert "seeded_no_mem_ctrl" in runner.METHODS and runner.a_ablation("seeded_no_mem_ctrl") is None
        assert runner._fault_of("seeded_no_mem_ctrl") is FAULTS["no_mem_ctrl"]
        assert runner.a_ablation("seeded_no_plan_check_no_mem_ctrl") == ("plan_check", "no_mem_ctrl")

    def test_the_older_ablations_are_untouched(self):
        for old in ("no_triage", "no_reuse", "one_checkout", "build_check"):
            assert old in runner.METHODS and runner.a_ablation(old) is None


class TestParsing:
    @pytest.mark.parametrize("part", PARTS)
    @pytest.mark.parametrize("name", sorted(FAULTS))
    def test_a_seeded_ablation_names_its_part_and_its_fault(self, part, name):
        assert runner.a_ablation(f"seeded_no_{part}_{name}") == (part, name)
        assert runner._fault_of(f"seeded_no_{part}_{name}") is FAULTS[name]

    @pytest.mark.parametrize("part", PARTS)
    def test_a_plain_ablation_names_its_part_and_no_fault(self, part):
        assert runner.a_ablation(f"no_{part}") == (part, None)
        assert runner._fault_of(f"no_{part}") is None

    @pytest.mark.parametrize("method", [
        "mace", "seeded_fpga_synth", "seeded_retry_crossbar", "faultcheck_drop_bist", "no_triage",
        "seeded_no_narrowing_nonesuch", "seeded_no_nonesuch_crossbar", "no_nonesuch",
    ])
    def test_other_methods_are_no_ablation(self, method):
        assert runner.a_ablation(method) is None

    def test_the_other_seeded_methods_still_resolve_to_their_faults(self):
        assert runner._fault_of("seeded_fpga_synth") is FAULTS["fpga_synth"]
        assert runner._fault_of("seeded_retry_crossbar") is FAULTS["crossbar"]
        assert runner._fault_of("faultcheck_drop_bist") is FAULTS["drop_bist"]

    def test_options_with_a_part_off(self):
        assert runner.a_options(LoopOptions(), "no_narrowing") == LoopOptions(narrowing=False)
        assert runner.a_options(LoopOptions(), "seeded_no_fast_path_crossbar") == LoopOptions(fast_path=False)
        assert runner.a_options(LoopOptions(), "no_gate_pass_ends_run") == LoopOptions(gate_pass_ends_run=False)
        assert runner.a_options(LoopOptions(), "mace") == LoopOptions()
        assert runner.a_options(LoopOptions(task_prompts=False), "no_plan_check") == LoopOptions(
            task_prompts=False, plan_check=False
        )


class TestJobs:
    def test_a_seeded_ablation_runs_on_the_tasks_its_fault_breaks(self):
        pico = suite_task("pico-task", core="pico")
        ariane = suite_task()
        jobs = runner.plan_jobs((ariane, pico), ("seeded_no_narrowing_drop_bist", "seeded_no_narrowing_fpga_synth"), 1)
        assert {j.key for j in jobs} == {
            ("pico-task", "seeded_no_narrowing_drop_bist", 0),
            ("pico-task", "seeded_no_narrowing_fpga_synth", 0),
            ("ariane-2x2-barrier", "seeded_no_narrowing_fpga_synth", 0),
        }

    def test_a_seeded_ablation_applies_to_the_same_tasks_as_the_seeded_run(self):
        tasks = (suite_task(), suite_task("pico-task", core="pico"), suite_task("row", mesh=(4, 1)))
        for name in FAULTS:
            plain = {(j.task.id, j.repeat) for j in runner.plan_jobs(tasks, (f"seeded_{name}",), 2)}
            ablated = {(j.task.id, j.repeat) for j in runner.plan_jobs(tasks, (f"seeded_no_plan_check_{name}",), 2)}
            assert plain == ablated

    def test_a_plain_ablation_runs_on_every_bring_up_task_and_repeats(self):
        jobs = runner.plan_jobs((suite_task(), suite_task("pico-task", core="pico")), ("no_fast_path",), 3)
        assert len(jobs) == 6

    def test_an_ablation_is_not_a_once_method_and_calls_an_llm(self):
        for method in ("no_narrowing", "seeded_no_narrowing_crossbar"):
            assert method not in runner.ONCE_METHODS and method not in runner.NO_LLM_METHODS


class TestRunning:
    def run(self, tmp_path, monkeypatch, method, task=None):
        calls = []
        monkeypatch.setattr(
            "mace.eval.runner.run_mace_loop", lambda roots, spec, llm, db, **kwargs: calls.append((roots, kwargs)) or "done"
        )
        assert runner.run_job(runner.Job(task or suite_task(), method, 0), make_env(tmp_path)) == "done"
        return calls

    def test_a_plain_ablation_runs_the_loop_with_that_part_off_on_both_checkouts(self, tmp_path, monkeypatch):
        ((roots, kwargs),) = self.run(tmp_path, monkeypatch, "no_narrowing")
        assert roots == ("/a", "/b")
        assert kwargs["options"] == LoopOptions(narrowing=False)
        assert kwargs["labels"].meta["ablation"] == "narrowing" and "fault" not in kwargs["labels"].meta
        assert kwargs.get("plan_hook") is None

    @pytest.mark.parametrize("part", PARTS)
    def test_each_ablation_turns_off_exactly_its_part(self, tmp_path, monkeypatch, part):
        ((_, kwargs),) = self.run(tmp_path, monkeypatch, f"no_{part}")
        options = kwargs["options"]
        assert getattr(options, part) is False
        assert all(getattr(options, other) is True for other in PARTS if other != part)

    def test_a_seeded_ablation_breaks_the_first_plan_with_the_part_off(self, tmp_path, monkeypatch):
        ((_, kwargs),) = self.run(tmp_path, monkeypatch, "seeded_no_fast_path_crossbar")
        assert kwargs["options"] == LoopOptions(fast_path=False)
        assert kwargs["labels"].meta["fault"] == "crossbar" and kwargs["labels"].meta["ablation"] == "fast_path"
        broken = kwargs["plan_hook"](0, (Task(id="t", deps=(), kind="config", spec="s"),))
        assert broken[0].network == "xbar_config"

    def test_the_full_loop_and_the_plain_seeded_run_keep_every_part_on(self, tmp_path, monkeypatch):
        ((_, mace),) = self.run(tmp_path, monkeypatch, "mace")
        ((_, seeded),) = self.run(tmp_path, monkeypatch, "seeded_fpga_synth")
        for kwargs in (mace, seeded):
            assert kwargs["options"] == LoopOptions()
            assert "ablation" not in kwargs["labels"].meta
