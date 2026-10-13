"""Tier-0 tests for mace.codesign.trades and examples/rtlparam_pick_trades.py.

Run:
    pytest mace/test/test_codesign_trades.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

from mace import metrics
from mace.codesign.rtl import RtlDesign, RtlSpace
from mace.codesign.rtlsearch import RtlEvaluation
from mace.codesign.trades import Effect, format_trades, pick_trades, sweep_effects
from mace.eval.suite import load_suite
from mace.metrics import RunLabels
from mace.spec import Budget, MaceSpec

SPACE = RtlSpace({
    "l2_mshr": (4, 8, 16), "scoreboard": (4, 8, 16), "dcache_wbuf": (4, 8, 16),
    "store_buffer": (2, 4, 8), "home_alloc": ("LOW", "MIXED"),
})
# Finish-time change and storage bits per tile of each single change, bits from the catalog.
EFFECTS = [
    Effect("l2_mshr", 16, -9000, 1232),
    Effect("l2_mshr", 4, 4000, -616),
    Effect("scoreboard", 4, 3000, -1448),
    Effect("dcache_wbuf", 16, -2000, 1176),
    Effect("store_buffer", 2, 500, -524),
    Effect("home_alloc", "LOW", -100, 0),
]


def row(design, time, simulated=1, idx=0):
    """An evaluations row; a time of None is a design that did not pass."""
    return dict(
        design=design.as_json(), passed=int(time is not None), sim_time=time, simulated=simulated, idx=idx,
        storage_bits=design.storage_delta(), hart_cycles=None,
    )


def rows_for(default_time, singles, extra=()):
    """Sweep rows: the default and each single change as (name, value, sim_time or None for a failure)."""
    rows = []
    designs = [(SPACE.default(), default_time)]
    designs += [(SPACE.design({n: v}), t) for n, v, t in singles]
    for index, (design, time) in enumerate(designs):
        rows.append(row(design, time, idx=index))
    return rows + list(extra)


class TestSweepEffects:
    def test_each_passing_single_change_gives_its_effect(self):
        rows = rows_for(1000, [("l2_mshr", 16, 900), ("scoreboard", 4, 1030)])
        default_time, effects = sweep_effects(rows, "sim_time", 4)
        assert default_time == 1000
        assert sorted((e.name, e.value, e.delta, e.bits) for e in effects) == [
            ("l2_mshr", 16, -100, 1232), ("scoreboard", 4, 30, -1448),
        ]

    def test_a_failed_rejected_or_multi_change_design_gives_no_effect(self):
        multi = SPACE.design({"l2_mshr": 16, "scoreboard": 4})
        rows = rows_for(
            1000, [("l2_mshr", 4, None), ("dcache_wbuf", 16, 990)],
            extra=[
                row(multi, 850),
                row(SPACE.design({"home_alloc": "LOW"}), None, simulated=0),
            ],
        )
        _, effects = sweep_effects(rows, "sim_time", 4)
        assert [(e.name, e.value) for e in effects] == [("dcache_wbuf", 16)]

    def test_a_sweep_whose_default_did_not_pass_gives_nothing(self):
        rows = rows_for(None, [("l2_mshr", 16, 900)])
        assert sweep_effects(rows, "sim_time", 4) == (None, [])


class TestComputeTime:
    def test_effects_are_changes_in_the_compute_time_not_in_sim_time(self):
        default = SPACE.default()
        slower_compute = SPACE.design({"home_alloc": "LOW"})
        rows = [
            dict(row(default, 1_000_000), hart_cycles=json.dumps({"p.c": {"0": [100, 5100], "1": [100, 5200]}})),
            # sim_time falls by 1%, the harts end 300 cycles later: an effect of the printing tail.
            dict(row(slower_compute, 990_000, idx=1), hart_cycles=json.dumps({"p.c": {"0": [100, 5400], "1": [100, 5500]}})),
        ]
        default_time, effects = sweep_effects(rows, "compute_cycles", 2)
        assert default_time == 5100
        assert [(e.name, e.value, e.delta) for e in effects] == [("home_alloc", "LOW", 300)]
        _, by_sim = sweep_effects(rows, "sim_time", 2)
        assert by_sim[0].delta == -10_000

    def test_a_row_with_a_missing_hart_gives_no_compute_time(self):
        rows = [dict(row(SPACE.default(), 1_000_000), hart_cycles=json.dumps({"p.c": {"0": [100, 5100]}}))]
        assert sweep_effects(rows, "compute_cycles", 2) == (None, [])


class TestPickTrades:
    def test_at_a_budget_of_zero_a_larger_buffer_is_paid_for_with_a_smaller_one(self):
        trades = pick_trades(EFFECTS, SPACE, budget_bits=0, k=10)
        assert trades[0].changes == (("home_alloc", "LOW"), ("l2_mshr", 16), ("scoreboard", 4))
        assert trades[0].predicted_delta == -9000 + 3000 - 100
        assert trades[0].storage_delta == 1232 - 1448
        assert all(t.storage_delta <= 0 for t in trades)

    def test_the_ranking_is_by_predicted_finish_time_then_by_added_storage(self):
        trades = pick_trades(EFFECTS, SPACE, budget_bits=0, k=10)
        keys = [(t.predicted_delta, t.storage_delta) for t in trades]
        assert keys == sorted(keys)

    def test_a_larger_buffer_with_nothing_to_pay_for_it_is_left_out(self):
        trades = pick_trades([Effect("l2_mshr", 16, -9000, 1232), Effect("store_buffer", 2, 500, -524)], SPACE, 0)
        assert trades == []

    def test_a_change_that_costs_nothing_is_a_design_of_one_change(self):
        trades = pick_trades([Effect("home_alloc", "LOW", -100, 0)], SPACE, 0)
        assert [t.changes for t in trades] == [(("home_alloc", "LOW"),)]

    def test_without_a_budget_the_over_budget_combinations_come_back(self):
        trades = pick_trades(EFFECTS, SPACE, budget_bits=None, k=3)
        assert trades[0].changes == (("dcache_wbuf", 16), ("home_alloc", "LOW"), ("l2_mshr", 16))
        assert trades[0].storage_delta == 1176 + 1232

    def test_a_combination_predicted_no_faster_than_the_default_is_left_out(self):
        trades = pick_trades([Effect("scoreboard", 4, 3000, -1448), Effect("store_buffer", 2, 500, -524)], SPACE, 0)
        assert trades == []

    def test_max_changes_limits_the_size_of_a_trade(self):
        trades = pick_trades(EFFECTS, SPACE, budget_bits=0, k=50, max_changes=2)
        assert max(len(t.changes) for t in trades) == 2

    def test_k_limits_the_number_of_trades(self):
        assert len(pick_trades(EFFECTS, SPACE, budget_bits=None, k=4)) == 4

    def test_a_trade_takes_at_most_one_value_of_a_parameter(self):
        for trade in pick_trades(EFFECTS, SPACE, budget_bits=None, k=100):
            names = [n for n, _ in trade.changes]
            assert len(names) == len(set(names))

    def test_an_effect_outside_the_space_is_ignored(self):
        small = RtlSpace({"l2_mshr": (8, 16), "scoreboard": (8,)})
        trades = pick_trades(EFFECTS, small, budget_bits=None, k=10)
        assert [t.changes for t in trades] == [(("l2_mshr", 16),)]

    def test_each_trade_is_a_design_of_the_space(self):
        for trade in pick_trades(EFFECTS, SPACE, budget_bits=0, k=10):
            assert SPACE.contains(trade.design)
            assert dict(trade.changes) == trade.design.changed()


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
      finish: sim_time
      space:
        l2_mshr: [4, 8, 16]
        scoreboard: [4, 8, 16]
        dcache_wbuf: [4, 8, 16]
        store_buffer: [2, 4, 8]
        home_alloc: [LOW, MIXED]
"""


class TestFormat:
    def test_the_printed_trades_load_as_the_task_s_trades(self, tmp_path):
        trades = pick_trades(EFFECTS, SPACE, budget_bits=0, k=3)
        text = format_trades(trades, 1_000_000, 0, 1.0)
        path = tmp_path / "suite.yaml"
        path.write_text(SUITE + textwrap.indent(text, "      ") + "\n")
        (task,) = load_suite(path)
        assert list(task.rtlparam.trades) == [t.design for t in trades]
        assert "# predicted -6100 (-0.6%), storage -216 bits" in text

    def test_the_header_says_the_prediction_is_not_a_result(self):
        text = format_trades(pick_trades(EFFECTS, SPACE, 0, k=1), 1_000_000, 0, 1.0)
        assert "default finish time 1000000; a budget of 0 bits (ratio 1.0)" in text
        assert "not a result" in text


def load_script():
    path = Path(__file__).resolve().parents[2] / "examples" / "rtlparam_pick_trades.py"
    spec = importlib.util.spec_from_file_location("rtlparam_pick_trades", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCommandLine:
    def _sweep(self, tmp_path, singles, status="passed", method="rtlparam_sweep", windows=None):
        """A sweep database. *windows* maps "default" and (name, value) to the cycles every hart works,
        written as four harts' start and end."""
        db = metrics.open_db(str(tmp_path / "rp.db"), ray_placement=False)
        spec = MaceSpec(
            workloads=("matmul_scaled.c",), objective="x", target_mesh=(2, 2), budget=Budget(max_wall_s=3600)
        )
        run_id = metrics.start_run(db, spec, labels=RunLabels(method=method, task="rp-task"))
        designs = [(SPACE.default(), 1_000_000)] + [(SPACE.design({n: v}), t) for n, v, t in singles]
        for index, (design, time) in enumerate(designs):
            changes = tuple(design.changed().items())
            window = None if windows is None else windows["default" if not changes else changes[0]]
            harts = {} if window is None else {"matmul_scaled.c": {h: (100, 100 + window) for h in range(4)}}
            metrics.record_rtl_evaluation(
                db, run_id,
                RtlEvaluation(
                    index=index, round=index, design=design, passed=time is not None, feasible=time is not None,
                    sim_time=time, storage_bits=design.storage_delta(), wall_s=1.0, hart_cycles=harts,
                ),
            )
        metrics.finish_run(db, run_id, status)
        return str(tmp_path / "rp.db")

    def _run(self, tmp_path, monkeypatch, capsys, db, extra=(), suite=SUITE):
        suite_path = tmp_path / "suite.yaml"
        suite_path.write_text(suite)
        monkeypatch.setattr(
            sys, "argv", ["x", "--task", "rp-task", "--db", db, "--suite", str(suite_path), *extra]
        )
        code = load_script().main()
        return code, capsys.readouterr()

    def test_prints_the_effects_and_the_trades_for_the_latest_sweep(self, tmp_path, monkeypatch, capsys):
        db = self._sweep(tmp_path, [
            ("l2_mshr", 16, 910_000), ("scoreboard", 4, 1_030_000), ("home_alloc", "LOW", 999_000),
            ("store_buffer", 2, 1_000_500),
        ])
        code, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert code == 0
        assert "# sweep run" in out.out and "4 passing single changes" in out.out
        assert "#   l2_mshr=16: -90000 (-9.00%), +1232 storage bits" in out.out
        assert "finish time is sim_time" in out.out
        assert "trades:" in out.out
        assert "  - {home_alloc: LOW, l2_mshr: 16, scoreboard: 4}  # predicted -61000 (-6.1%), storage -216 bits" in out.out

    def test_says_so_when_no_in_budget_combination_helps(self, tmp_path, monkeypatch, capsys):
        db = self._sweep(tmp_path, [("l2_mshr", 16, 1_010_000), ("scoreboard", 4, 1_030_000)])
        code, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert code == 0
        assert "no in-budget combination is predicted to beat the default design" in out.out
        assert "trades:" not in out.out

    def test_a_database_without_a_sweep_of_the_task_is_an_error(self, tmp_path, monkeypatch, capsys):
        db = self._sweep(tmp_path, [("l2_mshr", 16, 910_000)], method="rtlparam_mace")
        code, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert code == 1
        assert "no finished rtlparam_sweep run of rp-task" in out.err

    def test_a_sweep_whose_default_failed_is_an_error(self, tmp_path, monkeypatch, capsys):
        db = self._sweep(tmp_path, [("l2_mshr", 16, 910_000)])
        con = sqlite3.connect(db)
        con.execute("UPDATE evaluations SET passed = 0, feasible = 0, sim_time = NULL WHERE idx = 0")
        con.commit()
        con.close()
        code, out = self._run(tmp_path, monkeypatch, capsys, db)
        assert code == 1 and "default design has no finish time" in out.err

    def test_reads_the_compute_time_when_the_suite_asks_for_it(self, tmp_path, monkeypatch, capsys):
        """sim_time says l2_mshr=16 is 2% slower; the harts say it finishes 20% sooner."""
        windows = {"default": 5000, ("l2_mshr", 16): 4000, ("scoreboard", 4): 5500}
        db = self._sweep(
            tmp_path, [("l2_mshr", 16, 1_020_000), ("scoreboard", 4, 1_000_000)], windows=windows
        )
        suite = SUITE.replace("finish: sim_time", "finish: compute_cycles")
        code, out = self._run(tmp_path, monkeypatch, capsys, db, suite=suite)
        assert code == 0
        assert "finish time is compute_cycles" in out.out
        assert "#   l2_mshr=16: -1000 (-20.00%), +1232 storage bits" in out.out
        assert "  - {l2_mshr: 16, scoreboard: 4}  # predicted -500 (-10.0%), storage -216 bits" in out.out

    def test_a_task_that_is_not_an_rtl_parameter_task_is_refused(self, tmp_path, monkeypatch, capsys):
        db = self._sweep(tmp_path, [("l2_mshr", 16, 910_000)])
        suite = tmp_path / "suite.yaml"
        suite.write_text(SUITE)
        monkeypatch.setattr(sys, "argv", ["x", "--task", "missing", "--db", db, "--suite", str(suite)])
        with pytest.raises(SystemExit):
            load_script().main()
        assert "not an RTL-parameter task" in capsys.readouterr().err


def test_a_design_round_trips_through_the_sweep_json():
    design = SPACE.design({"l2_mshr": 16})
    assert RtlDesign.of(json.loads(design.as_json())["rtl"], tuple(SPACE.values)) == design
