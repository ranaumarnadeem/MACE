"""Tier-1 tests: real Ray, stub sims, for mace.orchestrator.run_mace_loop.

Run:
    pytest mace/test/cluster/orchestrator_e2e_test.py -q

run_mace_loop calls integrate_parallel, which dispatches real Ray tasks --
so even with a stub sims and FakeLLM, this needs real Ray (see
mace/test/cluster/integrator_e2e_test.py, which established the same
pattern for integrate_parallel itself). No real OpenPiton/hardware needed.
"""

from __future__ import annotations

import asyncio
import stat

import pytest

ray = pytest.importorskip("ray")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from chia_openpiton.test.conftest import STUB_SETTINGS, STUB_SIMS  # noqa: E402
from chia_openpiton.tools import PitonToolServer  # noqa: E402

import mace.integrator as integrator_mod  # noqa: E402
from mace.metrics import open_db, summary  # noqa: E402
from mace.orchestrator import run_mace_loop  # noqa: E402
from mace.report import generate_post_mortem  # noqa: E402
from mace.spec import Budget, MaceSpec  # noqa: E402
from mace.test.conftest import FakeLLM  # noqa: E402
from mace.triage import triage  # noqa: E402


@pytest.fixture(scope="module")
def ray_local():
    # address="local": forces a fresh local instance regardless of any stale
    # /tmp/ray/ray_current_cluster marker from an earlier torn-down cluster.
    ray.init(
        address="local",
        resources={"openpiton": 2, "fake_creds": 4},
        ignore_reinit_error=True,
        log_to_driver=False,
    )
    yield
    ray.shutdown()


def _make_stub_checkout(root, verdict: str = "pass", sim_log: bool = False) -> str:
    """A checkout whose stub sims always reports *verdict* -- verdict is
    baked into the script's own text (a file, correctly visible from any
    process), not read from an env var: see integrator_e2e_test.py's own
    _make_stub_checkout docstring for why monkeypatch.setenv doesn't work
    once Ray workers are involved. *sim_log* makes every run also write a
    real sim.log into its run_dir (FAKE_SIMS_BIG_SIM_LOG), for a test that
    reads a run's own files back."""
    tools_bin = root / "piton" / "tools" / "bin"
    tools_bin.mkdir(parents=True)
    (root / "build").mkdir()
    sims = tools_bin / "sims"
    shebang, _, body = STUB_SIMS.partition("\n")
    exports = f"export FAKE_SIMS_VERDICT={verdict}\n"
    if sim_log:
        exports += "export FAKE_SIMS_BIG_SIM_LOG=1\n"
    sims.write_text(f"{shebang}\n{exports}{body}")
    sims.chmod(sims.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (root / "piton" / "piton_settings.bash").write_text(STUB_SETTINGS)
    return str(root)


def _make_flaky_stub_checkout(root) -> str:
    """A checkout whose stub sims FAILS the first run it's ever asked for,
    then PASSES every run after -- for testing replan-to-success. State is
    a marker file (visible to any process), not an env var, for the same
    reason as above.
    """
    tools_bin = root / "piton" / "tools" / "bin"
    tools_bin.mkdir(parents=True)
    (root / "build").mkdir()
    marker = root / ".sims_run_called_once"
    shebang, _, body = STUB_SIMS.partition("\n")
    stateful_prefix = f"""\
case "$*" in
  *_run*)
    if [ ! -f "{marker}" ]; then
      touch "{marker}"
      export FAKE_SIMS_VERDICT=fail
    else
      export FAKE_SIMS_VERDICT=pass
    fi
    ;;
esac
"""
    sims = tools_bin / "sims"
    sims.write_text(f"{shebang}\n{stateful_prefix}\n{body}")
    sims.chmod(sims.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (root / "piton" / "piton_settings.bash").write_text(STUB_SETTINGS)
    return str(root)


def make_spec(**override):
    kwargs = {"workloads": ("hello_world.c",), "objective": "bring up 1x1 ariane"}
    kwargs.update(override)
    return MaceSpec(**kwargs)


TASK_LINE = "TASK: t1 | deps= | kind=workload | run hello_world.c\n"


class TestPassesImmediately:
    def test_one_iteration_no_failures(self, ray_local, tmp_path):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[TASK_LINE, "edit t1"])

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        assert result.status == "passed"
        assert len(result.iterations) == 1
        assert summary(db, result.run_id)["successful_tasks"] == 1
        assert summary(db, result.run_id)["failures_recovered"] == 0


class TestReplanToSuccess:
    def test_fails_once_then_replan_passes(self, ray_local, tmp_path):
        checkout = _make_flaky_stub_checkout(tmp_path / "openpiton")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(
            responses=[
                TASK_LINE,  # iteration 0 plan
                "DIAGNOSIS: config_error\nFIX: adjust the mesh\n",  # triage
                TASK_LINE,  # iteration 1 plan (replan)
                "unused",  # reserved for iteration 1's task prompt (remote dispatch;
                           # see FakeLLM's docstring -- it doesn't advance this queue,
                           # but needs it non-empty at dispatch time)
            ]
        )

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        assert result.status == "passed"
        assert len(result.iterations) == 2
        got = summary(db, result.run_id)
        assert got["successful_tasks"] == 1  # only the passing attempt is "successful"
        assert got["failures_recovered"] == 1

        failure_row = db.query_one(
            "SELECT diagnosis, fix, recovered FROM failures WHERE run_id = ?", (result.run_id,)
        )
        assert failure_row == {"diagnosis": "config_error", "fix": "adjust the mesh", "recovered": 1}

    def test_feedback_is_passed_to_the_replan_call(self, ray_local, tmp_path):
        checkout = _make_flaky_stub_checkout(tmp_path / "openpiton")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(
            responses=[
                TASK_LINE,
                "DIAGNOSIS: timeout\nFIX: raise rtl_timeout\n",
                TASK_LINE,
                "unused",  # reserved for iteration 1's task prompt -- see above
            ]
        )

        run_mace_loop((checkout,), make_spec(), llm, db)

        # llm.calls only ever reflects LOCAL calls (plan, triage) -- the task's
        # own prompt is dispatched via .chia_remote and recorded on a
        # separate, serialized copy of llm, invisible here. So this is the
        # THIRD local call: [plan0, triage0, plan1(replan)].
        replan_message, _ = llm.calls[2]
        assert "diagnosis=timeout" in replan_message
        assert "suggested fix=raise rtl_timeout" in replan_message


class TestNeverPasses:
    def test_exhausts_budget(self, ray_local, tmp_path):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="fail")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        budget = Budget(max_iterations=2)
        llm = FakeLLM(
            responses=[
                TASK_LINE, "DIAGNOSIS: rtl_suspect\nFIX: try again\n",
                TASK_LINE, "DIAGNOSIS: rtl_suspect\nFIX: try again\n",
                "ASSESSMENT: inconclusive\n",  # post-mortem, once budget is exhausted
            ]
        )

        result = run_mace_loop((checkout,), make_spec(budget=budget), llm, db)

        assert result.status == "budget_exceeded"
        assert len(result.iterations) == 2
        got = summary(db, result.run_id)
        assert got["successful_tasks"] == 0
        assert got["failures_recovered"] == 0  # never passed -- nothing to mark

    def test_unparseable_triage_falls_back_to_unknown(self, ray_local, tmp_path):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="fail")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        budget = Budget(max_iterations=1)
        llm = FakeLLM(
            responses=[
                TASK_LINE,
                "I'm not sure what happened.",  # no DIAGNOSIS: line -> TriageError
                "ASSESSMENT: inconclusive\n",  # post-mortem, once budget is exhausted
            ]
        )

        result = run_mace_loop((checkout,), make_spec(budget=budget), llm, db)

        assert result.status == "budget_exceeded"
        row = db.query_one(
            "SELECT diagnosis FROM failures WHERE run_id = ?", (result.run_id,)
        )
        assert row["diagnosis"] == "unknown"


class TestPlanningFailsOutright:
    def test_no_task_lines_stops_immediately(self, ray_local, tmp_path):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=["I don't have enough information to plan yet."])

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        assert result.status == "planning_failed"
        assert result.iterations == ()


class TestWallTimeBudget:
    def test_exceeded_before_first_iteration(self, ray_local, tmp_path, monkeypatch):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        budget = Budget(max_wall_s=1)
        llm = FakeLLM(responses=[])  # must never be called

        times = iter([0.0, 100.0])  # started=0, first check already past budget
        monkeypatch.setattr("mace.orchestrator.time.monotonic", lambda: next(times))

        result = run_mace_loop((checkout,), make_spec(budget=budget), llm, db)

        assert result.status == "budget_exceeded"
        assert result.iterations == ()
        assert llm.calls == []


class TestCostBudget:
    """extract_cost_usd itself is tested in isolation in mace/test/test_llm.py
    (TestExtractCostUsd) -- these tests monkeypatch it to a fixed value, the
    same way TestWallTimeBudget monkeypatches time.monotonic, to prove the
    accumulate-and-check logic in run_mace_loop independent of which LLM
    backend actually reported the cost."""

    def test_stops_when_accumulated_cost_exceeds_max_usd(self, ray_local, tmp_path, monkeypatch):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="fail")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        budget = Budget(max_iterations=5, max_usd=1.0)
        llm = FakeLLM(
            responses=[
                TASK_LINE, "DIAGNOSIS: rtl_suspect\nFIX: try again\n",
                TASK_LINE, "DIAGNOSIS: rtl_suspect\nFIX: try again\n",
                "ASSESSMENT: inconclusive\n",  # post-mortem, once budget is exhausted
            ]
        )
        monkeypatch.setattr("mace.orchestrator.extract_cost_usd", lambda query: 0.6)

        result = run_mace_loop((checkout,), make_spec(budget=budget), llm, db)

        assert result.status == "budget_exceeded"
        # iter0: total=0.6 (<=1.0, iter1 allowed); iter1: total=1.2 (iter2 blocked)
        assert len(result.iterations) == 2

    def test_iteration_usd_is_recorded_in_the_db(self, ray_local, tmp_path, monkeypatch):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[TASK_LINE, "edit t1"])
        monkeypatch.setattr("mace.orchestrator.extract_cost_usd", lambda query: 0.25)

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        assert result.status == "passed"
        row = db.query_one(
            "SELECT usd FROM iterations WHERE run_id = ? AND iteration = 0", (result.run_id,)
        )
        assert row["usd"] == 0.25


class TestReplayTagsReachIntegrateParallel:
    """integrate_parallel's own run_id/iteration wiring is tested directly in
    mace/test/cluster/integrator_e2e_test.py::TestReplayTagging -- this only
    proves run_mace_loop actually passes its run_id and the current
    iteration number through on each call, not just that the parameters
    exist."""

    def test_passes_its_own_run_id_and_iteration_number(self, ray_local, tmp_path, monkeypatch):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[TASK_LINE, "edit t1"])

        calls = []
        real_integrate_parallel = integrator_mod.integrate_parallel

        def _spy(*args, **kwargs):
            calls.append((kwargs.get("run_id"), kwargs.get("iteration")))
            return real_integrate_parallel(*args, **kwargs)

        monkeypatch.setattr("mace.orchestrator.integrate_parallel", _spy)

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        assert calls == [(result.run_id, 0)]


class TestOnIterationCallback:
    def test_called_once_per_iteration_with_that_iterations_results(self, ray_local, tmp_path):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[TASK_LINE, "edit t1"])
        calls = []

        result = run_mace_loop(
            (checkout,), make_spec(), llm, db, on_iteration=lambda i, r: calls.append((i, r))
        )

        assert result.status == "passed"
        assert len(calls) == 1
        assert calls[0][0] == 0
        assert calls[0][1] == result.iterations[0]

    def test_not_called_when_omitted(self, ray_local, tmp_path):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[TASK_LINE, "edit t1"])

        result = run_mace_loop((checkout,), make_spec(), llm, db)  # no on_iteration

        assert result.status == "passed"  # would have raised if the default broke anything


class TestPostMortem:
    """generate_post_mortem itself is tested in isolation in mace/test/
    test_report.py -- these prove run_mace_loop calls it in exactly the
    right circumstances: when the run genuinely tried and never passed, and
    never when it passed, hit a checksum mismatch, or never got a task DAG
    at all (see run_mace_loop's own docstring for why those are excluded)."""

    def test_generated_when_the_run_exhausts_its_budget(self, ray_local, tmp_path, monkeypatch):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="fail")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        budget = Budget(max_iterations=1)
        llm = FakeLLM(
            responses=[
                TASK_LINE, "DIAGNOSIS: rtl_suspect\nFIX: try again\n",
                "ASSESSMENT: likely_hardware_limitation\nEXPLANATION: x\nNEXT_STEPS: y\n",
            ]
        )

        result = run_mace_loop((checkout,), make_spec(budget=budget), llm, db)

        assert result.status == "budget_exceeded"
        assert result.post_mortem is not None
        assert result.post_mortem.assessment == "likely_hardware_limitation"
        from mace.metrics import get_post_mortem
        assert get_post_mortem(db, result.run_id) == result.post_mortem

    def test_not_generated_when_the_run_passes(self, ray_local, tmp_path):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[TASK_LINE, "edit t1"])  # no ASSESSMENT: response queued

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        assert result.status == "passed"
        assert result.post_mortem is None

    def test_not_generated_on_checksum_mismatch(self, ray_local, tmp_path, monkeypatch):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[])  # must never be called -- no ASSESSMENT: response queued

        def _tampered(*a, **kw):
            raise ValueError("checksum mismatch")

        monkeypatch.setattr("mace.orchestrator.verify_checksums", _tampered)

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        assert result.status == "checksum_mismatch"
        assert result.post_mortem is None

    def test_not_generated_when_planning_fails_outright(self, ray_local, tmp_path):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=["I don't have enough information to plan yet."])

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        assert result.status == "planning_failed"
        assert result.post_mortem is None

    def test_unparseable_post_mortem_is_dropped_not_raised(self, ray_local, tmp_path):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="fail")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        budget = Budget(max_iterations=1)
        llm = FakeLLM(
            responses=[
                TASK_LINE, "DIAGNOSIS: rtl_suspect\nFIX: try again\n",
                "I don't have a clear conclusion.",  # no ASSESSMENT: line -> ReportError
            ]
        )

        result = run_mace_loop((checkout,), make_spec(budget=budget), llm, db)

        assert result.status == "budget_exceeded"
        assert result.post_mortem is None


class TestChecksumMismatch:
    """verify_checksums itself is tested in isolation in mace/test/test_workloads.py
    -- these tests monkeypatch it to prove run_mace_loop actually calls it
    before spending anything, and stops cleanly when it raises."""

    def test_stops_before_any_llm_call_or_iteration(self, ray_local, tmp_path, monkeypatch):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[])  # must never be called

        def _tampered(*a, **kw):
            raise ValueError("checksum mismatch: barrier_atomic.c")

        monkeypatch.setattr("mace.orchestrator.verify_checksums", _tampered)

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        assert result.status == "checksum_mismatch"
        assert result.iterations == ()
        assert llm.calls == []

    def test_recorded_in_the_runs_table(self, ray_local, tmp_path, monkeypatch):
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="pass")
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[])

        def _tampered(*a, **kw):
            raise ValueError("checksum mismatch")

        monkeypatch.setattr("mace.orchestrator.verify_checksums", _tampered)

        result = run_mace_loop((checkout,), make_spec(), llm, db)

        row = db.query_one("SELECT status FROM runs WHERE run_id = ?", (result.run_id,))
        assert row["status"] == "checksum_mismatch"


def _grep_over_mcp(tool: PitonToolServer, pattern: str) -> str:
    """Grep *tool*'s run's sim.log through the tool's own MCP endpoint -- the
    URL a real backend's tool loop connects to (chia.models.vertex), so what
    answers is the copy its server actor holds, not this process's."""

    async def _call() -> str:
        url = f"http://{tool.hostname}:{tool.port}/{tool.name}/mcp"
        async with streamable_http_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    f"{tool.name}_grep", arguments={"source": "sim_log", "pattern": pattern}
                )
                return result.content[0].text

    return asyncio.run(_call())


class TestTriageToolServer:
    def test_triage_and_post_mortem_see_the_failed_run_over_mcp(self, ray_local, tmp_path, monkeypatch):
        """The triage tool server really runs in its own Ray actor here, and
        is only ever reached over MCP -- so a failed run that got no further
        than run_mace_loop's own copy of the tool shows up as "no run yet"."""
        checkout = _make_stub_checkout(tmp_path / "openpiton", verdict="fail", sim_log=True)
        db = open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(
            responses=[
                TASK_LINE, "DIAGNOSIS: rtl_suspect\nFIX: try again\n",
                TASK_LINE, "DIAGNOSIS: rtl_suspect\nFIX: try again\n",
                "ASSESSMENT: inconclusive\n",  # post-mortem, once budget is exhausted
            ]
        )
        calls = []

        def grepping(real):
            def _grepping(*args, tools=(), **kwargs):
                (tool,) = [t for t in tools if isinstance(t, PitonToolServer)]
                calls.append((real.__name__, tool, _grep_over_mcp(tool, "Simulation ->")))
                return real(*args, tools=tools, **kwargs)

            return _grepping

        monkeypatch.setattr("mace.orchestrator.triage", grepping(triage))
        monkeypatch.setattr("mace.orchestrator.generate_post_mortem", grepping(generate_post_mortem))

        result = run_mace_loop((checkout,), make_spec(budget=Budget(max_iterations=2)), llm, db)

        assert result.status == "budget_exceeded"
        assert [name for name, _, _ in calls] == ["triage", "triage", "generate_post_mortem"]
        for _, _, out in calls:
            assert "FAIL(HIT BAD TRAP)" in out
        (_, first, _), (_, second, _), (_, last, _) = calls
        assert second is not first and last is second  # one per triage; the post-mortem gets the latest
        assert first._server_actor is None and second._server_actor is None  # both really stopped
