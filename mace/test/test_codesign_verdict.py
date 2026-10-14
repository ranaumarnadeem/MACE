"""Tier-0 tests for mace.codesign.verdict.

Run:
    pytest mace/test/test_codesign_verdict.py -q

The rows are five designs of the 2x2 sweep on matmul_scaled.c, with their
simulation times and per-hart start and end cycles as the sweep recorded them.
"""

from __future__ import annotations

import json

import pytest

from mace.codesign.verdict import assess, compute_cycles, row_changes, row_finish

DEFAULT = {
    "dcache_wbuf": 8, "home_alloc": "MIXED", "l2_mshr": 8, "l2_req_buf": 8,
    "l2_req_data_buf": 4, "scoreboard": 8, "store_buffer": 4,
}
# (parameter changes, sim_time, storage bits, {hart: (start, end)})
SWEEP = [
    ({}, 143562750, 0, {0: (2780, 231461), 1: (2815, 231635), 2: (2785, 231371), 3: (2889, 231457)}),
    ({"dcache_wbuf": 4}, 142202250, -588, {0: (3041, 230906), 1: (3059, 230969), 2: (3051, 230887), 3: (3083, 231740)}),
    ({"home_alloc": "MIDDLE"}, 140638250, 0, {0: (2988, 229916), 1: (2984, 234840), 2: (3067, 232222), 3: (3079, 240048)}),
    ({"l2_req_buf": 4}, 141949750, -256, {0: (2786, 230715), 1: (2817, 231343), 2: (2779, 231168), 3: (2881, 231257)}),
    ({"scoreboard": 16}, 143562750, 2896, {0: (2780, 231461), 1: (2815, 231635), 2: (2785, 231371), 3: (2889, 231457)}),
]


def row(changes, sim_time, bits, harts, passed=1, simulated=1, program="matmul_scaled.c"):
    return dict(
        design=json.dumps({"rtl": {**DEFAULT, **changes}}), passed=passed, simulated=simulated, sim_time=sim_time,
        storage_bits=bits, hart_cycles=json.dumps({program: {str(h): list(v) for h, v in harts.items()}}),
    )


ROWS = [row(*entry) for entry in SWEEP]


class TestComputeCycles:
    def test_is_the_latest_end_minus_the_earliest_start(self):
        assert compute_cycles({"p.c": {0: (2780, 231461), 1: (2815, 231635)}}, 2) == 231635 - 2780

    def test_accepts_json_string_harts(self):
        assert compute_cycles({"p.c": {"0": [10, 100], "1": [12, 90]}}, 2) == 90

    def test_a_program_that_reported_fewer_harts_than_tiles_gives_none(self):
        """A missing hart could be the slowest."""
        assert compute_cycles({"p.c": {0: (1, 10), 1: (2, 12)}}, 4) is None

    def test_no_program_with_cycles_gives_none(self):
        assert compute_cycles({}, 4) is None

    def test_programs_add(self):
        harts = {"a.c": {0: (0, 100)}, "b.c": {0: (5, 50)}}
        assert compute_cycles(harts, 1) == 145

    def test_the_sweep_s_default_design(self):
        assert compute_cycles({"p.c": SWEEP[0][3]}, 4) == 228855


class TestRowFinish:
    def test_compute_cycles_comes_from_the_per_hart_data(self):
        assert row_finish(ROWS[0], "compute_cycles", 4) == 228855

    def test_sim_time_is_the_testbench_s(self):
        assert row_finish(ROWS[0], "sim_time", 4) == 143562750

    @pytest.mark.parametrize("override", [dict(passed=0), dict(simulated=0), dict(sim_time=None)])
    def test_a_design_that_did_not_pass_or_was_not_simulated_has_no_finish(self, override):
        assert row_finish({**ROWS[0], **override}, "compute_cycles", 4) is None
        assert row_finish({**ROWS[0], **override}, "sim_time", 4) is None

    def test_a_row_without_per_hart_data_has_no_compute_time(self):
        assert row_finish({**ROWS[0], "hart_cycles": None}, "compute_cycles", 4) is None
        assert row_finish({**ROWS[0], "hart_cycles": None}, "sim_time", 4) == 143562750

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="finish must be one of"):
            row_finish(ROWS[0], "wall_time", 4)


def test_row_changes_lists_the_non_default_parameters_sorted():
    both = row({"scoreboard": 16, "dcache_wbuf": 4}, 1, 0, {0: (1, 2)})
    assert row_changes(both) == (("dcache_wbuf", 4), ("scoreboard", 16))
    assert row_changes(ROWS[0]) == ()


class TestAssess:
    def test_compute_time_finds_no_design_that_beats_the_default_by_one_percent(self):
        verdict = assess(ROWS, budget_bits=0, margin=0.01, finish="compute_cycles", tiles=4)
        assert verdict.default_finish == 228855
        assert not verdict.go
        assert verdict.best.changes == (("l2_req_buf", 4),)
        assert verdict.best.gain == pytest.approx(291 / 228855)

    def test_the_same_rows_are_a_go_when_sim_time_is_the_finish_time(self):
        """The UART tail: home_alloc=MIDDLE ends 3.6% later and still shows a shorter sim_time."""
        verdict = assess(ROWS, budget_bits=0, margin=0.01, finish="sim_time", tiles=4)
        assert verdict.go
        assert verdict.best.changes == (("home_alloc", "MIDDLE"),)
        assert verdict.best.gain == pytest.approx((143562750 - 140638250) / 143562750)

    def test_a_looser_margin_turns_the_compute_verdict_into_a_go(self):
        assert assess(ROWS, 0, 0.001, "compute_cycles", 4).go

    def test_candidates_are_best_first_and_include_the_ones_that_lose(self):
        verdict = assess(ROWS, 0, 0.01, "compute_cycles", 4)
        assert [c.changes for c in verdict.candidates] == [
            (("l2_req_buf", 4),), (("dcache_wbuf", 4),), (("home_alloc", "MIDDLE"),),
        ]
        assert verdict.candidates[-1].gain < 0

    def test_a_design_over_the_storage_budget_is_left_out(self):
        verdict = assess(ROWS, budget_bits=0, margin=0.01, finish="compute_cycles", tiles=4)
        assert (("scoreboard", 16),) not in [c.changes for c in verdict.candidates]
        loose = assess(ROWS, budget_bits=None, margin=0.01, finish="compute_cycles", tiles=4)
        assert (("scoreboard", 16),) in [c.changes for c in loose.candidates]

    def test_a_design_that_failed_is_left_out(self):
        failed = [{**r, "passed": 0} if row_changes(r) == (("l2_req_buf", 4),) else r for r in ROWS]
        verdict = assess(failed, 0, 0.01, "compute_cycles", 4)
        assert (("l2_req_buf", 4),) not in [c.changes for c in verdict.candidates]

    def test_a_design_several_runs_simulated_counts_once(self):
        verdict = assess(ROWS + ROWS, 0, 0.01, "compute_cycles", 4)
        assert len(verdict.candidates) == 3

    def test_without_a_passing_default_there_is_nothing_to_compare_with(self):
        verdict = assess(ROWS[1:], 0, 0.01, "compute_cycles", 4)
        assert verdict.default_finish is None and verdict.candidates == () and not verdict.go

    def test_runs_of_the_default_that_disagree_are_flagged(self):
        other = row({}, 143562750, 0, {0: (2780, 231999), 1: (2815, 231635), 2: (2785, 231371), 3: (2889, 231457)})
        assert not assess(ROWS + [other], 0, 0.01, "compute_cycles", 4).default_consistent
        assert assess(ROWS + [ROWS[0]], 0, 0.01, "compute_cycles", 4).default_consistent

    def test_a_search_with_only_the_default_has_no_candidates(self):
        verdict = assess(ROWS[:1], 0, 0.01, "compute_cycles", 4)
        assert verdict.default_finish == 228855 and verdict.best is None and not verdict.go


# --- the command line ---------------------------------------------------------

import importlib.util  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from mace import metrics  # noqa: E402
from mace.codesign.rtl import RtlDesign  # noqa: E402
from mace.codesign.rtlsearch import RtlEvaluation  # noqa: E402
from mace.metrics import RunLabels  # noqa: E402
from mace.spec import Budget, MaceSpec  # noqa: E402

SUITE = """\
version: 1
defaults:
  budget: {max_iterations: 1, max_wall_s: 3600}
tasks:
  - id: rp-task
    core: ariane
    mesh: [2, 2]
    workloads: [matmul_scaled.c]
    objective: Finish matmul_scaled.c soonest.
    rtlparam:
      storage_budget_ratio: 1.0
      space:
        dcache_wbuf: [4, 8, 16]
        home_alloc: [LOW, MIDDLE, MIXED]
        l2_req_buf: [4, 8, 16]
        scoreboard: [4, 8, 16]
"""


def load_script():
    path = Path(__file__).resolve().parents[2] / "examples" / "rtlparam_go_nogo.py"
    spec = importlib.util.spec_from_file_location("rtlparam_go_nogo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCommandLine:
    def _db(self, tmp_path, name="rp.db", method="rtlparam_sweep", rows=SWEEP, task="rp-task"):
        db = metrics.open_db(str(tmp_path / name), ray_placement=False)
        spec = MaceSpec(workloads=("matmul_scaled.c",), objective="x", target_mesh=(2, 2), budget=Budget(max_wall_s=3600))
        run_id = metrics.start_run(db, spec, labels=RunLabels(method=method, task=task))
        for index, (changes, sim_time, bits, harts) in enumerate(rows):
            design = RtlDesign.of({**DEFAULT, **changes}, tuple(DEFAULT))
            metrics.record_rtl_evaluation(
                db, run_id,
                RtlEvaluation(
                    index=index, round=index, design=design, passed=True, feasible=True, sim_time=sim_time,
                    storage_bits=bits, wall_s=1.0, hart_cycles={"matmul_scaled.c": harts},
                ),
            )
        metrics.finish_run(db, run_id, "passed")
        return str(tmp_path / name)

    def _run(self, tmp_path, monkeypatch, capsys, *dbs, suite=SUITE, task="rp-task"):
        suite_path = tmp_path / "suite.yaml"
        suite_path.write_text(suite)
        argv = ["x", "--task", task, "--suite", str(suite_path)]
        for db in dbs:
            argv += ["--db", db]
        monkeypatch.setattr(sys, "argv", argv)
        code = load_script().main()
        return code, capsys.readouterr()

    def test_the_2x2_sweep_is_a_no_go_under_the_compute_time_and_says_why(self, tmp_path, monkeypatch, capsys):
        db = self._db(tmp_path)
        code, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert code == 0
        assert "finish time is compute_cycles; a budget of 0 storage bits (ratio 1.0); margin 1.00%" in out.out
        assert "default finish time 228855" in out.out
        assert "+0.13%  " in out.out and "l2_req_buf=4  (single)" in out.out
        assert "NO-GO: no in-budget design beats the default by 1.00%." in out.out
        assert "The best in-budget design is +0.13% against it." in out.out
        assert "The default parameters are balanced for this workload." in out.out
        assert "GO:" not in out.out.replace("NO-GO:", "")

    def test_the_same_sweep_is_a_go_when_the_suite_asks_for_sim_time(self, tmp_path, monkeypatch, capsys):
        db = self._db(tmp_path)
        suite = SUITE.replace("      storage_budget_ratio: 1.0\n", "      storage_budget_ratio: 1.0\n      finish: sim_time\n")
        code, out = self._run(tmp_path, monkeypatch, capsys, db, suite=suite)
        assert code == 0
        assert "GO: home_alloc=MIDDLE beats the default by 2.04%, within the budget." in out.out
        assert "NO-GO" not in out.out

    def test_a_run_of_only_the_default_design_gives_no_verdict_and_not_a_no_go(self, tmp_path, monkeypatch, capsys):
        db = self._db(tmp_path, method="rtlparam_default", rows=SWEEP[:1])
        code, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert code == 1
        assert "default finish time 228855" in out.out
        assert "NO VERDICT: no design other than the default has passed within the budget yet" in out.err
        assert "NO-GO" not in out.out

    def test_designs_over_the_budget_are_not_listed(self, tmp_path, monkeypatch, capsys):
        db = self._db(tmp_path)
        _, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert "scoreboard=16" not in out.out
        assert "3 in-budget designs passed on every tile" in out.out

    def test_runs_from_several_databases_are_combined(self, tmp_path, monkeypatch, capsys):
        first = self._db(tmp_path, "a.db", rows=SWEEP[:2])
        second = self._db(tmp_path, "b.db", method="rtlparam_trades", rows=[SWEEP[0], SWEEP[3]])
        _, out = self._run(tmp_path, monkeypatch, capsys, first, second)
        assert "2 in-budget designs passed on every tile" in out.out

    def test_a_trade_is_labelled_a_trade(self, tmp_path, monkeypatch, capsys):
        trade = ({"dcache_wbuf": 4, "scoreboard": 16}, 143562750, -588 + 2896, SWEEP[1][3])
        both_ways = ({"dcache_wbuf": 4, "l2_req_buf": 4}, 141000000, -588 - 256, {0: (3000, 229500), 1: (3000, 229600), 2: (3000, 229400), 3: (3000, 229500)})
        db = self._db(tmp_path, rows=[SWEEP[0], trade, both_ways])
        _, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert "dcache_wbuf=4, l2_req_buf=4  (trade)" in out.out

    def test_runs_of_other_tasks_are_ignored(self, tmp_path, monkeypatch, capsys):
        db = self._db(tmp_path, task="another-task")
        code, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert code == 1
        assert "NO VERDICT" in out.err

    def test_an_unknown_task_is_refused(self, tmp_path, monkeypatch, capsys):
        db = self._db(tmp_path)
        with pytest.raises(SystemExit):
            self._run(tmp_path, monkeypatch, capsys, db, task="missing")
        assert "not an RTL-parameter task" in capsys.readouterr().err

    def test_a_default_that_disagrees_with_itself_is_flagged(self, tmp_path, monkeypatch, capsys):
        drifted = ({}, 143562750, 0, {0: (2780, 231999), 1: (2815, 231635), 2: (2785, 231371), 3: (2889, 231457)})
        db = self._db(tmp_path, rows=[SWEEP[0], drifted, SWEEP[3]])
        _, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert "WARNING: runs of the default design disagree" in out.out
