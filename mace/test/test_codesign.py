"""Tier-0 tests for mace.codesign.

Run:
    pytest mace/test/test_codesign.py -q

Area uses CHIA's analytical model (no CACTI binary), and the build-and-check
path is a fake, so no simulator runs.
"""

from __future__ import annotations

import random

import pytest

from chia.base.llm_call import QueryResult
from chia_openpiton.state_def import DEFAULT_CACHES, PitonBuildArtifact, PitonConfig, PitonRunResult
from mace import metrics, planner
from mace.codesign import area, search
from mace.codesign.run import Evaluation, run_codesign
from mace.codesign.space import Design, DesignSpace, parse_design
from mace.spec import Budget, LoopOptions, MaceSpec, StepResult
from mace.test.conftest import FakeLLM

SPACE = DesignSpace(
    sizes={"l1d": (4096, 8192, 16384), "l2": (32768, 65536)},
    assocs={"l1d": (2, 4), "l2": (4, 8)},
    networks=("2dmesh_config", "xbar_config"),
)


def make_spec(**override):
    kwargs = {
        "workloads": ("matmul.c",),
        "objective": "Finish matmul.c soonest on a 2x2 mesh within the area budget.",
        "target_mesh": (2, 2),
        "budget": Budget(max_iterations=1, max_wall_s=3600),
    }
    kwargs.update(override)
    return MaceSpec(**kwargs)


class TestDesign:
    def test_missing_caches_keep_their_defaults(self):
        design = Design.of({"l1d": (4096, 2)})
        assert design.caches_dict() == {**DEFAULT_CACHES, "l1d": (4096, 2)}
        assert design.network == "2dmesh_config"

    def test_describe_and_parse_round_trip(self):
        design = Design.of({"l2": (32768, 8)}, "xbar_config")
        assert parse_design(design.describe()) == design

    @pytest.mark.parametrize("text", ["l1d=4096", "l3=4096,2", "network=torus", "l1d=4k,2"])
    def test_malformed_text_parses_to_none(self, text):
        assert parse_design(text) is None

    def test_a_design_becomes_a_config_task(self):
        task = Design.of({"l1d": (4096, 2)}, "xbar_config").task("d0")
        assert (task.id, task.kind, task.network) == ("d0", "config", "xbar_config")
        assert task.caches_dict["l1d"] == (4096, 2)


class TestDesignSpace:
    def test_count_and_membership(self):
        assert SPACE.count() == 3 * 2 * 2 * 2 * 2
        assert SPACE.contains(SPACE.design({"l1d_size": 4096, "l1d_assoc": 2, "network": "xbar_config"}))
        assert not SPACE.contains(Design.of({"l1d": (32768, 4)}))
        assert not SPACE.contains(Design.of({"l1i": (8192, 4)}))  # l1i is fixed

    def test_samples_stay_in_the_space(self):
        rng = random.Random(3)
        assert all(SPACE.contains(SPACE.sample(rng)) for _ in range(50))

    def test_grid_is_every_combination_in_order(self):
        grid = SPACE.grid({"l1d_size": (4096, 8192), "network": ("2dmesh_config", "xbar_config")})
        assert [(d.caches_dict()["l1d"][0], d.network) for d in grid] == [
            (4096, "2dmesh_config"), (4096, "xbar_config"), (8192, "2dmesh_config"), (8192, "xbar_config"),
        ]
        assert all(d.caches_dict()["l2"] == DEFAULT_CACHES["l2"] for d in grid)

    def test_grid_rejects_unknown_knobs(self):
        with pytest.raises(ValueError, match="outside the space"):
            SPACE.grid({"l1i_size": (8192,)})

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"sizes": {"l1d": (4096,)}, "assocs": {}},
            {"sizes": {"l3": (4096,)}, "assocs": {"l3": (2,)}},
            {"sizes": {"l1d": ()}, "assocs": {"l1d": (2,)}},
            {"sizes": {"l1d": (4096,)}, "assocs": {"l1d": (0,)}},
            {"networks": ("torus",)},
        ],
    )
    def test_invalid_spaces_raise(self, kwargs):
        with pytest.raises(ValueError):
            DesignSpace(**kwargs)

    def test_describe_names_the_fixed_caches(self):
        text = SPACE.describe()
        assert "- l1d: size in bytes one of 4096, 8192, 16384; associativity one of 2, 4" in text
        assert "fixed at their defaults" in text and "l1i=16384,4" in text


class TestArea:
    def test_tag_width_grows_with_associativity(self):
        _, tag2 = area.cache_arrays("l1d", 8192, 2)
        _, tag4 = area.cache_arrays("l1d", 8192, 4)
        assert tag4.width > tag2.width
        data, _ = area.cache_arrays("l1d", 8192, 4)
        assert (data.depth, data.width) == (512, 128)

    def test_a_partial_set_is_rejected(self):
        with pytest.raises(ValueError, match="whole number"):
            area.cache_arrays("l2", 1000, 4)

    def test_area_scales_with_tiles_and_names_its_source(self):
        design = Design.of({})
        one = area.design_area(design, 1, cacti="")
        four = area.design_area(design, 4, cacti="")
        assert four.area_um2 == pytest.approx(4 * one.area_um2)
        assert one.source == "analytical"

    def test_bigger_caches_cost_more_area(self):
        small = area.design_area(Design.of({"l2": (32768, 4)}), 1, cacti="")
        big = area.design_area(Design.of({"l2": (131072, 4)}), 1, cacti="")
        assert big.area_um2 > small.area_um2


def evaluation(design, passed=True, sim_time=1000, area_um2=1.0, index=0):
    return Evaluation(
        index=index, round=0, design=design, passed=passed, feasible=passed and sim_time is not None,
        sim_time=sim_time if passed else None, area_um2=area_um2, read_energy_nj=0.0,
        area_source="analytical", wall_s=1.0,
    )


class TestRandomAndGrid:
    def test_random_is_seeded_and_never_repeats(self):
        first = search.RandomSearch(SPACE, seed=5).propose([], 6)
        again = search.RandomSearch(SPACE, seed=5).propose([], 6)
        assert first == again
        assert len(set(first)) == 6
        history = [evaluation(d) for d in first]
        more = search.RandomSearch(SPACE, seed=5).propose(history, 6)
        assert not set(more) & set(first)

    def test_random_stops_when_the_space_is_used_up(self):
        tiny = DesignSpace(sizes={"l1d": (4096,)}, assocs={"l1d": (2,)})
        history = [evaluation(tiny.design({}))]
        assert search.RandomSearch(tiny).propose(history, 2) == []

    def test_grid_hands_out_its_designs_in_order(self):
        grid = SPACE.grid({"l1d_size": (4096, 8192, 16384)})
        strategy = search.GridSearch(grid)
        assert strategy.propose([], 2) == grid[:2]
        assert strategy.propose([evaluation(grid[0])], 2) == grid[1:3]


class TestLLMProposer:
    def _proposer(self, replies):
        return search.LLMProposer(FakeLLM(responses=replies), make_spec(), SPACE, area_budget_um2=5000.0)

    def test_the_prompt_states_the_inputs_the_space_and_the_history(self):
        proposer = self._proposer([])
        tried = SPACE.design({"l1d_size": 4096})
        prompt = proposer.build_prompt([evaluation(tried, sim_time=777, area_um2=6000.0)], 2)
        assert planner.render_inputs(make_spec()) in prompt
        assert SPACE.describe() in prompt
        assert "5000 square microns" in prompt
        assert f"- {tried.describe()}: passed, finish time 777, area 6000 (over budget)" in prompt

    def test_reads_valid_new_designs_and_skips_the_rest(self):
        good = SPACE.design({"l1d_size": 16384, "network": "xbar_config"})
        tried = SPACE.design({})
        outside = Design.of({"l1d": (65536, 4)})
        reply = "\n".join(
            [f"DESIGN: {tried.describe()}", f"DESIGN: {outside.describe()}", "DESIGN: nonsense",
             f"DESIGN: {good.describe()}", f"DESIGN: {good.describe()}"]
        )
        proposer = self._proposer([reply])
        assert proposer.propose([evaluation(tried)], 2) == [good]

    def test_each_round_is_recorded_as_a_propose_call(self):
        from mace import usage

        log = usage.UsageLog()
        with usage.recording(log):
            self._proposer(["no designs today"]).propose([], 2)
        assert [c.phase for c in log.calls()] == ["propose"]


class TestBayesianSearch:
    def test_proposes_new_designs_from_the_space_and_learns_from_outcomes(self):
        pytest.importorskip("optuna")
        strategy = search.BayesianSearch(SPACE, seed=1)
        first = strategy.propose([], 2)
        assert len(first) == 2 and all(SPACE.contains(d) for d in first)
        history = [evaluation(first[0], sim_time=500, index=0), evaluation(first[1], passed=False, index=1)]
        strategy.observe(history)
        second = strategy.propose(history, 2)
        assert not set(second) & set(first)
        assert strategy.worst == 500


def fake_step(task, passed, sim_time):
    build = PitonBuildArtifact(
        success=True, returncode=0, config=PitonConfig(), sim_type="vlt", model_dir="/x",
        binary_path="/x/V", wall_time_s=10.0,
    )
    run = PitonRunResult(
        success=passed, returncode=0, test="matmul.c", sim_type="vlt", run_dir="/x",
        verdict="pass" if passed else "timeout", sim_time=sim_time if passed else None, wall_time_s=5.0,
    )
    query = QueryResult(result="", returncode=0, stderr="", stream_result="", success=True)
    return StepResult(task=task, query=query, build=build, run=run, passed=passed, runs=(run,))


class TestRunCodesign:
    @pytest.fixture(autouse=True)
    def _no_nodes(self, monkeypatch):
        monkeypatch.setattr("mace.codesign.run.open_nodes", lambda roots: [])
        monkeypatch.setattr("mace.codesign.run.close_nodes", lambda nodes: None)

    def _integrate(self, monkeypatch, outcome, received):
        def fake(piton_roots, spec, tasks, llm, **kwargs):
            received.append((tasks, kwargs))
            return tuple(fake_step(t, *outcome(t)) for t in tasks)

        monkeypatch.setattr("mace.codesign.run.integrate_parallel", fake)

    def test_runs_the_simulation_budget_in_batches_and_records_each_design(self, tmp_path, monkeypatch):
        received = []
        self._integrate(monkeypatch, lambda t: (True, 1000 - len(t.spec)), received)
        db = metrics.open_db(str(tmp_path / "cd.db"), ray_placement=False)

        result = run_codesign(
            ("/a", "/b"), make_spec(), search.RandomSearch(SPACE, seed=2), db, simulations=5, batch=2,
            area_fn=lambda design, tiles: area.design_area(design, tiles, cacti=""),
        )

        assert [len(tasks) for tasks, _ in received] == [2, 2, 1]
        assert received[0][1]["options"] == LoopOptions(task_prompts=False)
        assert len(result.evaluations) == 5
        assert result.status == "passed"
        rows = db.query("SELECT idx, round, passed, feasible, area_source FROM evaluations WHERE run_id = ? ORDER BY idx", (result.run_id,))
        assert [(r["idx"], r["round"]) for r in rows] == [(0, 0), (1, 0), (2, 1), (3, 1), (4, 2)]
        assert all(r["area_source"] == "analytical" for r in rows)
        assert db.query_value("SELECT method FROM runs WHERE run_id = ?", (result.run_id,)) == "codesign_random"

    def test_a_design_over_the_area_budget_is_not_feasible(self, tmp_path, monkeypatch):
        self._integrate(monkeypatch, lambda t: (True, 1000), [])
        db = metrics.open_db(str(tmp_path / "cd.db"), ray_placement=False)
        result = run_codesign(
            ("/a",), make_spec(), search.GridSearch(SPACE.grid({"l2_size": (32768, 65536)})), db,
            simulations=2, batch=1, area_budget_um2=1.0,
            area_fn=lambda design, tiles: area.design_area(design, tiles, cacti=""),
        )
        assert all(e.passed and not e.feasible for e in result.evaluations)
        assert result.status == "budget_exceeded"
        assert result.best is None

    def test_best_is_the_soonest_feasible_finish(self, tmp_path, monkeypatch):
        finish = {"4096": 900, "8192": 700, "16384": 800}
        self._integrate(monkeypatch, lambda t: (True, finish[t.spec.split("l1d=")[1].split(",")[0]]), [])
        db = metrics.open_db(str(tmp_path / "cd.db"), ray_placement=False)
        grid = SPACE.grid({"l1d_size": (4096, 8192, 16384)})
        result = run_codesign(("/a",), make_spec(), search.GridSearch(grid), db, simulations=3, batch=3,
                              area_fn=lambda design, tiles: area.design_area(design, tiles, cacti=""))
        assert result.best.design == grid[1]
        assert result.best.sim_time == 700

    def test_stops_when_the_strategy_has_nothing_new(self, tmp_path, monkeypatch):
        self._integrate(monkeypatch, lambda t: (False, None), [])
        db = metrics.open_db(str(tmp_path / "cd.db"), ray_placement=False)
        grid = SPACE.grid({"network": ("2dmesh_config",)})
        result = run_codesign(("/a",), make_spec(), search.GridSearch(grid), db, simulations=10, batch=2,
                              area_fn=lambda design, tiles: area.design_area(design, tiles, cacti=""))
        assert len(result.evaluations) == 1
        assert result.status == "budget_exceeded"

    def test_a_changed_gate_program_stops_before_any_design(self, tmp_path, monkeypatch):
        def mismatch():
            raise ValueError("checksum mismatch")

        monkeypatch.setattr("mace.codesign.run.verify_checksums", mismatch)
        db = metrics.open_db(str(tmp_path / "cd.db"), ray_placement=False)
        result = run_codesign(("/a",), make_spec(), search.RandomSearch(SPACE), db)
        assert (result.status, result.evaluations) == ("checksum_mismatch", ())
