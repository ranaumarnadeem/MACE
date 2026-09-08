"""Tier-1 tests: real Ray, stub sims, for mace.orchestrator.run_mace_loop.

Run:
    pytest mace/test/cluster/orchestrator_e2e_test.py -q

run_mace_loop calls integrate_parallel, which dispatches real Ray tasks --
so even with a stub sims and FakeLLM, this needs real Ray (see
mace/test/cluster/integrator_e2e_test.py, which established the same
pattern for integrate_parallel itself). No real OpenPiton/hardware needed.
"""

from __future__ import annotations

import stat

import pytest

ray = pytest.importorskip("ray")

from chia_openpiton.test.conftest import STUB_SETTINGS, STUB_SIMS  # noqa: E402

from mace.metrics import open_db, summary  # noqa: E402
from mace.orchestrator import run_mace_loop  # noqa: E402
from mace.spec import Budget, MaceSpec  # noqa: E402
from mace.test.conftest import FakeLLM  # noqa: E402


@pytest.fixture(scope="module")
def ray_local():
    ray.init(
        resources={"openpiton": 2, "fake_creds": 4},
        ignore_reinit_error=True,
        log_to_driver=False,
    )
    yield
    ray.shutdown()


def _make_stub_checkout(root, verdict: str = "pass") -> str:
    """A checkout whose stub sims always reports *verdict* -- verdict is
    baked into the script's own text (a file, correctly visible from any
    process), not read from an env var: see integrator_e2e_test.py's own
    _make_stub_checkout docstring for why monkeypatch.setenv doesn't work
    once Ray workers are involved."""
    tools_bin = root / "piton" / "tools" / "bin"
    tools_bin.mkdir(parents=True)
    (root / "build").mkdir()
    sims = tools_bin / "sims"
    shebang, _, body = STUB_SIMS.partition("\n")
    sims.write_text(f"{shebang}\nexport FAKE_SIMS_VERDICT={verdict}\n{body}")
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
