"""Tier-0 tests for mace.codesign.rtlsearch.

Run:
    pytest mace/test/test_rtlsearch.py -q

The workspace node is a fake over a synthetic checkout: its apply_edits writes
the files, its build regenerates the L2 header from the .pyv source as PyHP
does, and its runs report a finish time that depends on the L2 MSHR count.
No simulator or Ray runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from chia_openpiton.state_def import PitonBuildArtifact, PitonRunResult
from mace import metrics, usage
from mace.codesign import search
from mace.codesign.rtl import RtlSpace
from mace.codesign.rtlsearch import RtlEvaluation, RtlProposer, hart_table, run_rtlsearch
from mace.spec import Budget, MaceSpec
from mace.test.conftest import FakeLLM
from mace.test.test_codesign_rtl import synthetic_checkout

SPACE = RtlSpace({"l2_mshr": (4, 8, 16), "dcache_wbuf": (4, 8, 16), "home_alloc": ("LOW", "MIXED")})
HEADER = "piton/design/include/l2.h.pyv"
GENERATED = "piton/design/include/l2.tmp.h"


def make_spec(**override):
    kwargs = {
        "workloads": ("matmul_scaled.c",),
        "objective": "Finish matmul_scaled.c soonest on a 2x2 mesh.",
        "target_mesh": (2, 2),
        "budget": Budget(max_iterations=1, max_wall_s=3600),
    }
    kwargs.update(override)
    return MaceSpec(**kwargs)


class _Attr:
    def __init__(self, fn):
        self.chia_remote = fn


class FakeNode:
    """A workspace node over the checkout at *root*."""

    def __init__(self, root, regenerate=True, fail_run=lambda mshr: False):
        self.root = Path(root)
        self.regenerate = regenerate
        self.fail_run = fail_run
        self.backup: dict[str, str] = {}
        self.reverts = 0
        self.builds: list[str] = []
        self.runs = 0
        self.apply_edits = _Attr(self._apply)
        self.revert_edits = _Attr(self._revert)
        self.build = _Attr(self._build)
        self.run = _Attr(self._run)

    def mshr(self) -> int:
        return int(re.search(r"L2_MSHR_ENTRIES\s+(\d+)", (self.root / HEADER).read_text()).group(1))

    def _apply(self, edits, staged=""):
        for path, text in edits.items():
            self.backup[path] = (self.root / path).read_text()
            (self.root / path).write_text(text)
        return {"edits": dict(edits), "diff": f"diff of {sorted(edits)}"}

    def _revert(self):
        self.reverts += 1
        for path, text in self.backup.items():
            (self.root / path).write_text(text)
        self.backup = {}
        return []

    def _build(self, config, clean=False, timeout_seconds=None):
        self.builds.append(config.source_edits)
        if self.regenerate:
            (self.root / GENERATED).write_text((self.root / HEADER).read_text())
        return PitonBuildArtifact(
            success=True, returncode=0, config=config, sim_type="vlt", model_dir="/x",
            binary_path="/x/Vcmp_top", wall_time_s=20.0,
        )

    def _run(self, config, program, **kwargs):
        self.runs += 1
        failed = self.fail_run(self.mshr())
        uart = "".join(f"hart {h} start 100 end {1000 + 50 * h + 10 * self.mshr()} mismatches 0\n" for h in range(4))
        return PitonRunResult(
            success=not failed, returncode=0, test=program, sim_type="vlt", run_dir="/x/runs/1",
            verdict="fail" if failed else "pass", sim_time=None if failed else 100_000 - 1000 * self.mshr(),
            wall_time_s=3.0, fake_uart=uart,
        )


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "piton"
    for path, text in synthetic_checkout().items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


@pytest.fixture
def harness(tmp_path, checkout, monkeypatch):
    node = FakeNode(checkout)
    monkeypatch.setattr("mace.codesign.rtlsearch.get", lambda ref: ref)
    monkeypatch.setattr("mace.codesign.rtlsearch.open_nodes", lambda roots: [node])
    monkeypatch.setattr("mace.codesign.rtlsearch.close_nodes", lambda nodes: None)
    db = metrics.open_db(str(tmp_path / "rtl.db"), ray_placement=False)
    return node, db, str(checkout)


class Scripted:
    """A strategy that proposes a fixed list of designs, in order, and records what it hears."""

    name = "scripted"

    def __init__(self, designs):
        self.designs = list(designs)
        self.heard: list[RtlEvaluation] = []

    def propose(self, history, n):
        seen = {e.design for e in history}
        return [d for d in self.designs if d not in seen][:n]

    def observe(self, evaluations):
        self.heard += evaluations


def evaluation(design, index=0, sim_time=1000, passed=True, harts=None, **kw):
    return RtlEvaluation(
        index=index, round=index, design=design, passed=passed, feasible=passed and sim_time is not None,
        sim_time=sim_time, storage_bits=design.storage_delta(), wall_s=1.0,
        hart_cycles=harts if harts is not None else {}, **kw,
    )


class TestRunRtlsearch:
    def test_checks_each_design_and_records_it_with_its_per_hart_cycles(self, harness):
        node, db, root = harness
        designs = [SPACE.default(), SPACE.design({"l2_mshr": 16}), SPACE.design({"dcache_wbuf": 4})]

        result = run_rtlsearch((root,), make_spec(), Scripted(designs), db, simulations=3)

        assert result.status == "passed"
        assert [e.design for e in result.evaluations] == designs
        assert [e.sim_time for e in result.evaluations] == [92_000, 84_000, 92_000]
        assert result.best.design == designs[1]
        rows = db.query(
            "SELECT idx, simulated, feasible, sim_time, storage_bits, hart_cycles, design FROM evaluations "
            "WHERE run_id = ? ORDER BY idx", (result.run_id,),
        )
        assert [r["sim_time"] for r in rows] == [92_000, 84_000, 92_000]
        assert [r["storage_bits"] for r in rows] == [0, 8 * 154, -4 * 147]
        harts = json.loads(rows[0]["hart_cycles"])
        assert harts["matmul_scaled.c"]["3"] == [100, 1000 + 150 + 80]
        assert json.loads(rows[1]["design"]) == {"rtl": {"dcache_wbuf": 8, "home_alloc": "MIXED", "l2_mshr": 16}}

    def test_each_distinct_design_builds_with_its_own_source_edits_and_the_default_with_none(self, harness):
        node, db, root = harness
        designs = [SPACE.default(), SPACE.design({"l2_mshr": 16}), SPACE.design({"l2_mshr": 4})]
        run_rtlsearch((root,), make_spec(), Scripted(designs), db, simulations=3)
        assert node.builds[0] == ""
        assert node.builds[1] and node.builds[2] and node.builds[1] != node.builds[2]

    def test_the_edits_are_reverted_after_every_design_and_the_checkout_is_left_as_it_was(self, harness, checkout):
        node, db, root = harness
        before = {p: (checkout / p).read_text() for p in synthetic_checkout()}
        run_rtlsearch((root,), make_spec(), Scripted([SPACE.default(), SPACE.design({"l2_mshr": 16})]), db,
                      simulations=2)
        assert {p: (checkout / p).read_text() for p in before} == before
        assert node.reverts >= 3  # before each design, and after the edited one

    def test_the_edits_are_reverted_when_the_build_raises(self, harness, checkout):
        node, db, root = harness
        before = (checkout / HEADER).read_text()

        def boom(config, clean=False, timeout_seconds=None):
            raise RuntimeError("worker died")

        node.build = _Attr(boom)
        with pytest.raises(RuntimeError, match="worker died"):
            run_rtlsearch((root,), make_spec(), Scripted([SPACE.design({"l2_mshr": 16})]), db, simulations=2)
        assert (checkout / HEADER).read_text() == before
        assert db.query("SELECT status FROM runs")[0]["status"] == "error"

    def test_the_storage_budget_rejects_a_design_without_a_simulation(self, harness):
        node, db, root = harness
        big = SPACE.design({"l2_mshr": 16})
        designs = [SPACE.default(), big, SPACE.design({"l2_mshr": 4})]
        strategy = Scripted(designs)

        result = run_rtlsearch((root,), make_spec(), strategy, db, simulations=2, storage_budget_bits=1000)

        assert [(e.simulated, e.rejected) for e in result.evaluations] == [
            (True, ""), (False, "over the storage budget"), (True, ""),
        ]
        assert node.runs == 2 and len(node.builds) == 2
        assert result.evaluations[1].sim_time is None and not result.evaluations[1].feasible
        assert [e.design for e in strategy.heard] == designs
        rows = db.query("SELECT simulated, rejected FROM evaluations ORDER BY idx")
        assert [(r["simulated"], r["rejected"]) for r in rows][1] == (0, "over the storage budget")

    def test_a_rejected_proposal_does_not_use_up_a_simulation(self, harness):
        node, db, root = harness
        many_big = [SPACE.design({"l2_mshr": 16, "dcache_wbuf": 16, "home_alloc": h}) for h in ("LOW", "MIXED")]
        designs = [SPACE.default(), *many_big, SPACE.design({"dcache_wbuf": 4})]
        result = run_rtlsearch((root,), make_spec(), Scripted(designs), db, simulations=2, storage_budget_bits=1)
        assert sum(e.simulated for e in result.evaluations) == 2
        assert len(result.evaluations) == 4

    def test_an_edit_that_does_not_apply_is_rejected_without_a_build(self, harness, checkout):
        node, db, root = harness
        (checkout / "piton/design/chip/tile/ariane/core/include/wt_cache_pkg.sv").write_text("// other version\n")
        result = run_rtlsearch(
            (root,), make_spec(), Scripted([SPACE.default(), SPACE.design({"dcache_wbuf": 4})]), db, simulations=2
        )
        bad = result.evaluations[1]
        assert not bad.simulated and "an edit does not apply" in bad.rejected and "dcache_wbuf" in bad.rejected
        assert len(node.builds) == 1

    def test_a_build_without_the_edit_in_its_generated_header_is_not_counted_as_a_design(self, harness):
        node, db, root = harness
        node.regenerate = False
        result = run_rtlsearch((root,), make_spec(), Scripted([SPACE.design({"l2_mshr": 16})]), db, simulations=1)
        e = result.evaluations[0]
        assert not e.simulated and not e.feasible
        assert e.rejected == "the build lacks the edit in piton/design/include/l2.tmp.h"
        assert node.runs == 0

    def test_a_design_that_fails_the_check_uses_a_simulation_and_is_not_feasible(self, harness):
        node, db, root = harness
        node.fail_run = lambda mshr: mshr == 4
        result = run_rtlsearch(
            (root,), make_spec(), Scripted([SPACE.default(), SPACE.design({"l2_mshr": 4})]), db, simulations=2
        )
        failed = result.evaluations[1]
        assert failed.simulated and not failed.passed and not failed.feasible and failed.sim_time is None
        assert result.best.design == SPACE.default()

    def test_a_search_with_no_passing_design_ends_budget_exceeded(self, harness):
        node, db, root = harness
        node.fail_run = lambda mshr: True
        result = run_rtlsearch((root,), make_spec(), Scripted([SPACE.default()]), db, simulations=1)
        assert result.status == "budget_exceeded" and result.best is None

    def test_a_strategy_with_nothing_new_twice_ends_the_search(self, harness):
        node, db, root = harness
        result = run_rtlsearch((root,), make_spec(), Scripted([SPACE.default()]), db, simulations=5)
        assert len(result.evaluations) == 1 and node.runs == 1

    def test_the_design_and_its_edit_land_in_the_task_row(self, harness):
        node, db, root = harness
        result = run_rtlsearch((root,), make_spec(), Scripted([SPACE.design({"l2_mshr": 16})]), db, simulations=1)
        (row,) = db.query("SELECT spec, kind, rtl_edits, passed FROM tasks WHERE run_id = ?", (result.run_id,))
        assert row["spec"] == "dcache_wbuf=8 home_alloc=MIXED l2_mshr=16" and row["kind"] == "config"
        assert "include/l2.h.pyv" in row["rtl_edits"] and row["passed"] == 1

    def test_a_changed_workload_ends_the_search_before_any_build(self, harness, monkeypatch):
        node, db, root = harness

        def mismatch():
            raise ValueError("matmul_scaled.c changed")

        monkeypatch.setattr("mace.codesign.rtlsearch.verify_checksums", mismatch)
        result = run_rtlsearch((root,), make_spec(), Scripted([SPACE.default()]), db, simulations=1)
        assert result.status == "checksum_mismatch" and node.builds == []

    def test_the_run_is_labelled_for_the_report(self, harness):
        node, db, root = harness
        result = run_rtlsearch((root,), make_spec(), Scripted([SPACE.default()]), db, simulations=1)
        assert db.query("SELECT method FROM runs WHERE run_id = ?", (result.run_id,))[0]["method"] == "rtlparam_scripted"

    def test_the_proposer_s_note_is_recorded_with_its_design(self, harness):
        node, db, root = harness
        llm = FakeLLM(responses=["BOTTLENECK: tile 3 waits on its L2 slice\nDESIGN: l2_mshr=16"])
        proposer = RtlProposer(llm, make_spec(), SPACE, None)
        result = run_rtlsearch((root,), make_spec(), proposer, db, simulations=2)
        rows = db.query("SELECT idx, note FROM evaluations ORDER BY idx")
        assert [r["note"] for r in rows] == ["", "tile 3 waits on its L2 slice"]
        assert result.evaluations[1].design == SPACE.design({"l2_mshr": 16})

    def test_the_first_round_is_the_default_design_with_no_llm_call(self, harness):
        node, db, root = harness
        llm = FakeLLM(responses=["no design"])
        result = run_rtlsearch((root,), make_spec(), RtlProposer(llm, make_spec(), SPACE, None), db, simulations=1)
        assert result.evaluations[0].design == SPACE.default()
        assert llm.calls == []
        assert db.query("SELECT COUNT(*) AS n FROM llm_calls")[0]["n"] == 0


class TestHartTable:
    HARTS = {"matmul_scaled.c": {0: (100, 1100), 1: (104, 1604), 2: (98, 1198), 3: (101, 1101)}}

    def test_lists_each_hart_with_its_tile_and_window(self):
        text = hart_table(self.HARTS, (2, 2))
        assert "matmul_scaled.c: hart tile start end window" in text
        assert "  1 (1, 0) 104 1604 1500" in text
        assert "  2 (0, 1) 98 1198 1100" in text

    def test_summarises_the_spread(self):
        text = hart_table(self.HARTS, (2, 2))
        assert "4 of 4 harts reported" in text
        assert "latest end minus earliest start 1506" in text
        assert "slowest window hart 1 (1, 0) (1500)" in text
        assert "fastest hart 0 (1000)" in text

    def test_says_how_many_harts_reported_when_some_are_missing(self):
        assert "2 of 16 harts reported" in hart_table({"p.c": {0: (1, 10), 5: (2, 12)}}, (4, 4))

    def test_a_program_without_per_hart_lines_adds_nothing(self):
        assert hart_table({"matmul.c": {}}, (2, 2)) == ""


class TestRtlProposer:
    def _proposer(self, replies=(), budget=None):
        return RtlProposer(FakeLLM(responses=list(replies)), make_spec(), SPACE, budget)

    def test_the_first_proposal_is_the_default_design(self):
        assert self._proposer().propose([], 1) == [SPACE.default()]

    def test_the_prompt_states_the_inputs_the_space_the_budget_and_the_history(self):
        from mace import planner

        tried = SPACE.design({"l2_mshr": 4})
        prompt = self._proposer(budget=2000).build_prompt([evaluation(tried, sim_time=777)])
        assert planner.render_inputs(make_spec()) in prompt
        assert SPACE.describe() in prompt
        assert "at most 2000 flip-flop bits per tile" in prompt
        assert "1. l2_mshr=4: passed, finish time 777, -616 storage bits" in prompt
        assert "hart h is on tile (x, y) = (h mod 2, h div 2)" in prompt

    def test_the_default_design_is_listed_as_such(self):
        prompt = self._proposer().build_prompt([evaluation(SPACE.default())])
        assert "1. the default design: passed" in prompt

    def test_there_is_no_budget_paragraph_without_a_budget(self):
        assert "Storage budget" not in self._proposer().build_prompt([evaluation(SPACE.default())])

    def test_a_rejected_design_is_listed_with_its_reason(self):
        rejected = evaluation(SPACE.design({"l2_mshr": 16}), sim_time=None, passed=False, simulated=False,
                              rejected="over the storage budget")
        prompt = self._proposer().build_prompt([evaluation(SPACE.default()), rejected])
        assert "rejected without a simulation (over the storage budget)" in prompt

    def test_the_prompt_shows_per_hart_cycles_for_the_best_and_the_latest_design(self):
        harts = {"matmul_scaled.c": {0: (1, 100), 1: (1, 200), 2: (1, 100), 3: (1, 100)}}
        best = evaluation(SPACE.design({"l2_mshr": 16}), index=0, sim_time=500, harts=harts)
        worse = evaluation(SPACE.design({"l2_mshr": 4}), index=1, sim_time=900, harts=harts)
        prompt = self._proposer().build_prompt([best, worse])
        assert "Per-hart cycles of design 1 (best finish time so far; l2_mshr=16)" in prompt
        assert "Per-hart cycles of design 2 (most recent; l2_mshr=4)" in prompt

    def test_one_design_that_is_both_best_and_latest_is_shown_once(self):
        harts = {"matmul_scaled.c": {0: (1, 100)}}
        prompt = self._proposer().build_prompt([evaluation(SPACE.default(), harts=harts)])
        assert prompt.count("Per-hart cycles of design") == 1

    def test_reads_the_first_valid_new_design_and_the_bottleneck_line(self):
        tried = SPACE.default()
        reply = "\n".join([
            "The hart on tile 3 is slowest.",
            "BOTTLENECK: tile 3 stalls on L2 misses",
            f"DESIGN: {tried.describe()}",
            "DESIGN: l2_mshr=32",
            "DESIGN: nonsense",
            "DESIGN: l2_mshr=16 dcache_wbuf=4 home_alloc=LOW",
            "DESIGN: l2_mshr=4",
        ])
        proposer = self._proposer([reply])
        [design] = proposer.propose([evaluation(tried)], 1)
        assert design == SPACE.design({"l2_mshr": 16, "dcache_wbuf": 4, "home_alloc": "LOW"})
        assert proposer.notes[design] == "tile 3 stalls on L2 misses"

    def test_a_reply_without_a_valid_design_proposes_nothing(self):
        assert self._proposer(["I cannot decide."]).propose([evaluation(SPACE.default())], 1) == []

    def test_each_round_is_recorded_as_a_propose_call(self):
        log = usage.UsageLog()
        with usage.recording(log):
            self._proposer(["no designs today"]).propose([evaluation(SPACE.default())], 1)
        assert [c.phase for c in log.calls()] == ["propose"]


class TestStrategiesOverRtl:
    def test_random_search_draws_new_designs_from_the_space(self):
        strategy = search.RandomSearch(SPACE, seed=3)
        history = []
        for index in range(6):
            [design] = strategy.propose(history, 1)
            assert SPACE.contains(design) and design not in {e.design for e in history}
            history.append(evaluation(design, index=index))

    def test_grid_search_walks_the_one_at_a_time_designs_starting_with_the_default(self):
        strategy = search.GridSearch(SPACE.one_at_a_time())
        [first] = strategy.propose([], 1)
        assert first == SPACE.default()

    def test_bayesian_search_asks_the_default_design_first_and_learns_its_score(self):
        pytest.importorskip("optuna")
        strategy = search.BayesianSearch(SPACE, seed=1, n_startup_trials=3, first=dict(SPACE.default().values))
        [first] = strategy.propose([], 1)
        assert first == SPACE.default()
        history = [evaluation(first, sim_time=500)]
        strategy.observe(history)
        assert strategy.study.trials[0].value == 500.0
        [second] = strategy.propose(history, 1)
        assert SPACE.contains(second) and second != first

    def test_bayesian_search_without_the_new_options_behaves_as_before(self):
        pytest.importorskip("optuna")
        strategy = search.BayesianSearch(SPACE, seed=1)
        assert strategy.study.sampler._n_startup_trials == 10
        [design] = strategy.propose([], 1)
        assert SPACE.contains(design)

    def test_the_startup_trial_count_reaches_the_sampler(self):
        pytest.importorskip("optuna")
        assert search.BayesianSearch(SPACE, n_startup_trials=3).study.sampler._n_startup_trials == 3
