"""Tier-0 tests for mace.narrowing.

Run:
    pytest mace/test/test_narrowing.py -q
"""

from __future__ import annotations

import pytest

from chia.base.llm_call import QueryResult
from chia_openpiton.state_def import DEFAULT_CACHES, PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import narrowing
from mace.loop import _config_for_task
from mace.narrowing import (
    ADDED_DEFINE,
    CACHE,
    COMBINATION,
    CULPRIT,
    MISSING_DEFINE,
    NETWORK,
    NO_CHANGES,
    NOT_CONFIG,
    SKIPPED,
    UNRESOLVED,
    Change,
    config_changes,
    narrow,
    reference_config,
    task_for_config,
    undo,
)
from mace.spec import MaceSpec, StepResult, Task

BIST = "CONFIG_DISABLE_BIST_CLEAR"
FPGA = "PITON_FPGA_SYNTH"


def spec(core="ariane", workloads=("barrier_atomic.c",)):
    return MaceSpec(workloads=workloads, objective="o", core=core, target_mesh=(2, 2))


def config(core="ariane", defines=("MINIMAL_MONITORING",), caches=None, network="2dmesh_config"):
    return PitonConfig(
        core=core, x_tiles=2, y_tiles=2, config_rtl=tuple(defines), network_config=network,
        caches={**DEFAULT_CACHES, **(caches or {})},
    )


def result(cfg, passed=False, build_ok=True, reason="", kind="config", task_id="t1", edits=None, build_s=10.0, run_s=5.0):
    build = PitonBuildArtifact(
        success=build_ok, returncode=0 if build_ok else 1, config=cfg, sim_type="vlt", model_dir="/x",
        binary_path="/x/V", wall_time_s=build_s, failure_reason=reason if not build_ok else "",
    )
    run = (
        PitonRunResult(success=passed, returncode=0, test="a.c", sim_type="vlt", run_dir="/x",
                       verdict="pass" if passed else "timeout", wall_time_s=run_s)
        if build_ok else None
    )
    query = QueryResult(result="", returncode=0, stderr="", stream_result="", success=True)
    return StepResult(
        task=Task(id=task_id, deps=(), kind=kind, spec="the instruction"), query=query, build=build, run=run,
        passed=passed, edits=edits,
    )


class Runner:
    """A run_tasks stand-in: a probe passes when *passes(config)* says so. Keeps every batch it was given."""

    def __init__(self, spec_, passes, not_started=False):
        self.spec, self.passes, self.not_started, self.batches = spec_, passes, not_started, []

    def __call__(self, tasks):
        self.batches.append(tasks)
        out = []
        for task in tasks:
            cfg = _config_for_task(self.spec, task)
            if self.not_started:
                out.append(result(cfg, build_ok=False, reason="budget_exhausted", task_id=task.id))
            else:
                out.append(result(cfg, passed=self.passes(cfg), task_id=task.id))
        return tuple(out)

    @property
    def tasks(self):
        return [t for batch in self.batches for t in batch]


def run_narrow(failed_cfg, passes, core="ariane", required=(), parallelism=2, time_left=10_000.0, **kwargs):
    s = spec(core)
    runner = Runner(s, passes)
    failed = result(failed_cfg, **kwargs)
    outcome = narrow(s, failed, tuple(required), runner, parallelism, lambda: time_left)
    return outcome, runner


class TestConfigChanges:
    def test_the_reference_has_no_changes_from_itself(self):
        ref = reference_config(spec())
        assert config_changes(ref, ref) == ()

    def test_an_added_define_is_a_change(self):
        ref = reference_config(spec())
        assert config_changes(config(defines=("MINIMAL_MONITORING", FPGA)), ref) == (Change(ADDED_DEFINE, FPGA),)

    def test_a_default_define_the_config_leaves_out_is_a_change(self):
        ref = reference_config(spec())
        assert config_changes(config(defines=()), ref) == (Change(MISSING_DEFINE, "MINIMAL_MONITORING"),)

    def test_a_required_define_the_config_leaves_out_is_a_change(self):
        ref = reference_config(spec("pico"), (BIST,))
        assert config_changes(config("pico"), ref) == (Change(MISSING_DEFINE, BIST),)

    def test_a_required_define_the_config_has_is_not_a_change(self):
        ref = reference_config(spec("pico"), (BIST,))
        assert config_changes(config("pico", defines=("MINIMAL_MONITORING", BIST)), ref) == ()

    def test_a_cache_off_its_default_and_the_crossbar_are_changes(self):
        ref = reference_config(spec())
        changes = config_changes(config(caches={"l1d": (4096, 2)}, network="xbar_config"), ref)
        assert changes == (Change(CACHE, "l1d", (4096, 2)), Change(NETWORK, "network", ("xbar_config",)))

    def test_changes_come_in_a_fixed_order(self):
        ref = reference_config(spec())
        cfg = config(defines=(FPGA,), caches={"l2": (32768, 4), "l15": (8192, 8)}, network="xbar_config")
        kinds = [(c.kind, c.name) for c in config_changes(cfg, ref)]
        assert kinds == [
            (ADDED_DEFINE, FPGA), (MISSING_DEFINE, "MINIMAL_MONITORING"), (CACHE, "l15"), (CACHE, "l2"), (NETWORK, "network"),
        ]

    def test_each_change_says_what_it_does_what_undoing_builds_and_the_remedy(self):
        added, missing = Change(ADDED_DEFINE, FPGA), Change(MISSING_DEFINE, BIST)
        cache, net = Change(CACHE, "l1d", (8192, 8)), Change(NETWORK, "network", ("xbar_config",))
        assert added.describe() == f"adds the RTL define {FPGA}" and added.undone() == f"without the RTL define {FPGA}"
        assert added.remedy() == f"leave out the RTL define {FPGA}"
        assert missing.describe() == f"leaves out the RTL define {BIST}" and missing.remedy() == f"keep the RTL define {BIST}"
        assert cache.describe() == "sets l1d to 8192,8 (default 8192,4)" and cache.undone() == "with l1d at 8192,4"
        assert cache.remedy() == "keep l1d at its default 8192,4"
        assert net.describe() == "uses the xbar_config interconnect" and net.remedy() == "keep the default interconnect"


class TestUndoAndTaskForConfig:
    CONFIGS = [
        config(),
        config(defines=("MINIMAL_MONITORING", FPGA)),
        config(defines=()),
        config(defines=(BIST,)),
        config(caches={"l1d": (4096, 2), "l2": (32768, 8)}),
        config(network="xbar_config"),
        config("pico", defines=(BIST, "MINIMAL_MONITORING"), caches={"l15": (16384, 4)}),  # sorted, as built configs are
    ]

    @pytest.mark.parametrize("cfg", CONFIGS)
    def test_a_task_built_for_a_config_gives_the_same_build_flags(self, cfg):
        s = spec(cfg.core)
        task = task_for_config(Task(id="t", deps=(), kind="workload", spec="the instruction"), cfg, "narrow-t-0")
        assert _config_for_task(s, task).sims_flags() == cfg.sims_flags()
        assert (task.id, task.kind, task.spec, task.deps) == ("narrow-t-0", "workload", "the instruction", ())

    def test_undoing_every_change_gives_the_reference(self):
        s = spec()
        ref = reference_config(s, ())
        cfg = config(defines=(FPGA,), caches={"l1d": (4096, 2)}, network="xbar_config")
        assert undo(cfg, config_changes(cfg, ref), ref).sims_flags() == ref.sims_flags()

    def test_undoing_one_change_leaves_the_others(self):
        ref = reference_config(spec(), ())
        cfg = config(defines=("MINIMAL_MONITORING", FPGA), caches={"l1d": (4096, 2)})
        undone = undo(cfg, (Change(ADDED_DEFINE, FPGA),), ref)
        assert undone.config_rtl == ("MINIMAL_MONITORING",) and undone.caches["l1d"] == (4096, 2)

    def test_undoing_a_missing_required_define_adds_it(self):
        ref = reference_config(spec("pico"), (BIST,))
        undone = undo(config("pico"), (Change(MISSING_DEFINE, BIST),), ref)
        assert set(undone.config_rtl) == {"MINIMAL_MONITORING", BIST}


class TestNarrow:
    def test_a_task_with_no_changes_is_not_probed(self):
        outcome, runner = run_narrow(config(), lambda cfg: True)
        assert outcome.verdict == NO_CHANGES and runner.batches == [] and outcome.summary() == ""

    @pytest.mark.parametrize("kwargs", [
        {"kind": "unit_test"}, {"edits": {"a.v": "text"}}, {"build_ok": False, "reason": "way_rule"},
        {"build_ok": False, "reason": "budget_exhausted"},
    ])
    def test_a_failure_no_config_change_explains_is_skipped(self, kwargs):
        outcome, runner = run_narrow(config(defines=("MINIMAL_MONITORING", FPGA)), lambda cfg: True, **kwargs)
        assert outcome.verdict == SKIPPED and runner.batches == []

    def test_one_change_that_when_undone_passes_is_the_culprit(self):
        # The seeded fpga_synth fault: the added define breaks the build, and the default builds.
        outcome, runner = run_narrow(config(defines=("MINIMAL_MONITORING", FPGA)), lambda cfg: FPGA not in cfg.config_rtl, build_ok=False, reason="verilog_error")
        assert outcome.verdict == CULPRIT and outcome.definitive
        assert outcome.culprits == ((Change(ADDED_DEFINE, FPGA),),)
        assert outcome.fix() == f"leave out the RTL define {FPGA}"
        assert len(runner.tasks) == 1 and runner.tasks[0].id == "narrow-t1-0"
        assert f"without the RTL define {FPGA}: passed" in outcome.summary()

    def test_a_missing_required_define_is_found_by_adding_it(self):
        # The seeded drop_bist fault on PicoRV32: the simulation times out without the define.
        outcome, _ = run_narrow(config("pico"), lambda cfg: BIST in cfg.config_rtl, core="pico", required=(BIST,))
        assert outcome.verdict == CULPRIT and outcome.fix() == f"keep the RTL define {BIST}"

    def test_one_change_whose_undoing_does_not_help_means_the_defaults_fail_too(self):
        outcome, _ = run_narrow(config(defines=("MINIMAL_MONITORING", FPGA)), lambda cfg: False)
        assert outcome.verdict == NOT_CONFIG and not outcome.definitive and outcome.fix() == ""
        assert "default configuration fails the same way" in outcome.summary()

    def test_with_two_changes_each_is_undone_alone_and_the_culprit_is_the_one_that_helps(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA), caches={"l1d": (4096, 2)})
        outcome, runner = run_narrow(cfg, lambda c: FPGA not in c.config_rtl)
        assert outcome.verdict == CULPRIT and outcome.culprits == ((Change(ADDED_DEFINE, FPGA),),)
        assert len(runner.tasks) == 2  # the all-undone probe is not needed
        assert [p.passed for p in outcome.probes] == [True, False]

    def test_two_changes_that_each_pass_when_undone_are_both_culprits(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA), network="xbar_config")
        outcome, _ = run_narrow(cfg, lambda c: True)
        assert outcome.verdict == CULPRIT and len(outcome.culprits) == 2
        assert outcome.fix() == f"leave out the RTL define {FPGA}; keep the default interconnect"

    def test_when_no_single_undo_passes_but_undoing_all_does_the_verdict_is_a_combination(self):
        # The retry agent's l1d_eight_way recovery: an 8-way L1.5 times out alone, an 8-way L1D alone is refused.
        cfg = config(caches={"l1d": (8192, 8), "l15": (8192, 8)})
        outcome, runner = run_narrow(cfg, lambda c: c.caches["l1d"] == (8192, 4) and c.caches["l15"] == (8192, 4))
        assert outcome.verdict == COMBINATION and outcome.definitive
        assert outcome.culprits == (config_changes(cfg, reference_config(spec())),)
        assert len(runner.tasks) == 3  # two alone, then both
        assert outcome.fix().startswith("build the default configuration instead of this combination")

    def test_the_combination_note_claims_only_what_the_probes_showed(self):
        # Two changes that each break the design alone give the same probes as two that break it only together:
        # every single undo still fails, and undoing both passes.
        cfg = config(defines=("MINIMAL_MONITORING", FPGA, "A_B"))
        outcome, _ = run_narrow(cfg, lambda c: c.sims_flags() == reference_config(spec()).sims_flags())
        note = outcome.summary()
        assert "still leaves the design failing" in note and "makes it pass" in note
        assert "No single change breaks" not in note and "together do" not in note

    def test_when_the_defaults_fail_too_the_changes_are_not_the_cause(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA), caches={"l1d": (4096, 2)})
        outcome, runner = run_narrow(cfg, lambda c: False)
        assert outcome.verdict == NOT_CONFIG and len(runner.tasks) == 3

    def test_more_changes_than_the_single_probe_limit_go_straight_to_all_undone(self):
        cfg = config(
            defines=("MINIMAL_MONITORING", FPGA, "A_B", "C_D"),
            caches={"l1d": (4096, 2), "l2": (32768, 4)}, network="xbar_config",
        )
        outcome, runner = run_narrow(cfg, lambda c: c.sims_flags() == reference_config(spec()).sims_flags())
        assert len(config_changes(cfg, reference_config(spec()))) > narrowing.MAX_SINGLE_PROBES
        assert outcome.verdict == COMBINATION and len(runner.tasks) == 1

    def test_probes_run_in_batches_no_larger_than_the_checkouts(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA, "A_B", "C_D"))
        _, runner = run_narrow(cfg, lambda c: False, parallelism=2)
        assert [len(b) for b in runner.batches] == [2, 1, 1]  # three singles in two batches, then all undone
        assert [t.id for t in runner.tasks] == ["narrow-t1-0", "narrow-t1-1", "narrow-t1-2", "narrow-t1-3"]

    def test_one_checkout_runs_the_probes_one_at_a_time(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA, "A_B"))
        _, runner = run_narrow(cfg, lambda c: False, parallelism=1)
        assert [len(b) for b in runner.batches] == [1, 1, 1]

    def test_no_time_means_no_probe_and_no_conclusion(self):
        outcome, runner = run_narrow(config(defines=("MINIMAL_MONITORING", FPGA)), lambda c: True, time_left=100.0)
        assert outcome.verdict == UNRESOLVED and runner.batches == [] and outcome.summary() == ""

    def test_a_probe_needs_twice_the_failed_tasks_own_time_to_start(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA))
        slow, _ = run_narrow(cfg, lambda c: True, time_left=1500.0, build_s=900.0, run_s=100.0)  # cycle 1000, needs 2000
        fast, _ = run_narrow(cfg, lambda c: True, time_left=1500.0, build_s=300.0, run_s=100.0)  # cycle 400, needs 800
        assert slow.verdict == UNRESOLVED and fast.verdict == CULPRIT

    def test_a_quick_failed_build_still_reserves_the_minimum_cycle(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA))
        outcome, _ = run_narrow(cfg, lambda c: True, time_left=2 * narrowing.MIN_CYCLE_S - 1, build_s=1.0, run_s=0.0)
        assert outcome.verdict == UNRESOLVED

    def test_time_that_runs_out_after_the_first_batch_leaves_the_rest_unrun(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA, "A_B", "C_D"))
        s = spec()
        clock = iter([10_000.0, 100.0, 100.0, 100.0])
        runner = Runner(s, lambda c: False)
        outcome = narrow(s, result(cfg), (), runner, 2, lambda: next(clock))
        assert [len(b) for b in runner.batches] == [2] and outcome.verdict == UNRESOLVED
        assert [p.ran for p in outcome.probes] == [True, True, False, False]

    def test_a_probe_that_never_started_is_no_evidence(self):
        s = spec()
        runner = Runner(s, lambda c: True, not_started=True)
        outcome = narrow(s, result(config(defines=("MINIMAL_MONITORING", FPGA))), (), runner, 2, lambda: 10_000.0)
        assert outcome.verdict == UNRESOLVED and outcome.summary() == ""

    def test_a_combination_found_before_every_change_was_tried_alone_says_so(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA, "A_B", "C_D"))
        s = spec()
        clock = iter([10_000.0, 100.0, 10_000.0])  # time for the first batch and the all-undone probe, not the second batch
        runner = Runner(s, lambda c: c.sims_flags() == reference_config(s).sims_flags())
        outcome = narrow(s, result(cfg), (), runner, 2, lambda: next(clock))
        assert outcome.verdict == COMBINATION and outcome.singles_unrun
        assert "ran out before each change could be put back alone" in outcome.summary()

    def test_failing_probes_are_kept_for_the_caller(self):
        cfg = config(defines=("MINIMAL_MONITORING", FPGA), caches={"l1d": (4096, 2)})
        outcome, _ = run_narrow(cfg, lambda c: FPGA not in c.config_rtl)
        failing = [p for p in outcome.probes if p.ran and not p.passed]
        assert len(failing) == 1 and failing[0].result.build.config.caches["l1d"] == (8192, 4)
        assert FPGA in failing[0].result.build.config.config_rtl
