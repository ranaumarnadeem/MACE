"""Tier-0 tests for a MACE run on the generic tile (mace.generic_core): the
spec's core descriptor, the build configuration it gives each task, and the
support check that ends an unsupported core's run before any LLM call.

Run:
    pytest mace/test/test_generic_core.py -q
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from chia_openpiton.core_descriptor import (
    FLIST_CORE,
    LINK_LD,
    SKELETON_MARK,
    TILE_DIR,
    load_descriptor,
    wrapper_skeleton,
)
from chia_openpiton.state_def import PitonRunResult
from mace import metrics
from mace.baselines.retry_agent import run_retry_agent
from mace.generic_core import boot_failures, config_fields, unsupported_reasons, unwritten_wrapper
from mace.loop import _config_for_task
from mace.orchestrator import run_mace_loop
from mace.spec import Budget, LoopOptions, MaceSpec, Task
from mace.test.conftest import FakeLLM
from mace.test.test_orchestrator import fake_integrate_parallel, step_result
from mace.triage import failure_evidence

REPO = Path(__file__).resolve().parents[2]
PICORV32 = REPO / "examples" / "cores" / "picorv32.yaml"


def descriptor_file(tmp_path, **override) -> str:
    data = yaml.safe_load(PICORV32.read_text())
    data.update(override)
    path = tmp_path / f"core_{len(list(tmp_path.glob('core_*.yaml')))}.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


def generic_spec(descriptor: str, **override) -> MaceSpec:
    kwargs = {
        "workloads": ("addi.S",),
        "objective": "Verify addi.S on a 2x2 mesh of the generic core.",
        "core": "generic",
        "core_descriptor": descriptor,
        "target_mesh": (2, 2),
        "budget": Budget(max_iterations=1),
    }
    kwargs.update(override)
    return MaceSpec(**kwargs)


@pytest.fixture
def checkout(tmp_path) -> Path:
    """A checkout patched with fixes 14-17 that holds PicoRV32's files."""
    root = tmp_path / "openpiton"
    for rel in (FLIST_CORE, LINK_LD):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
    tile = root / TILE_DIR
    for rel in ("pico/rtl/picorv32.v", "generic/cores/picorv32/generic_core_wrapper.v"):
        (tile / rel).parent.mkdir(parents=True, exist_ok=True)
        (tile / rel).write_text("// rtl\n")
    return root


def runs(db):
    return [r["status"] for r in db.query("SELECT status FROM runs")]


class TestSpec:
    def test_generic_needs_a_descriptor(self):
        with pytest.raises(ValueError, match="needs a core_descriptor"):
            MaceSpec(workloads=("addi.S",), objective="x", core="generic")

    def test_other_cores_take_no_descriptor(self):
        with pytest.raises(ValueError, match="only it takes one"):
            MaceSpec(workloads=("addi.S",), objective="x", core="pico", core_descriptor=str(PICORV32))


class TestConfig:
    def test_a_task_s_config_carries_the_descriptor_s_isa_and_digest(self):
        spec = generic_spec(str(PICORV32))
        cfg = _config_for_task(spec, Task(id="t1", deps=(), kind="workload", spec="addi.S"))
        flags = cfg.sims_flags()
        assert "-generic" in flags
        assert "-rv32_march=rv32ima" in flags and "-rv32_mabi=ilp32" in flags
        assert cfg.generic_core.startswith("picorv32@")

    def test_two_descriptors_build_two_models(self, tmp_path):
        task = Task(id="t1", deps=(), kind="workload", spec="addi.S")
        a = _config_for_task(generic_spec(str(PICORV32)), task)
        b = _config_for_task(generic_spec(descriptor_file(tmp_path, reset_vector=0x4000_0040)), task)
        assert a.build_id != b.build_id

    def test_other_cores_get_nothing_extra(self):
        spec = MaceSpec(workloads=("addi.S",), objective="x", core="pico")
        assert config_fields(spec) == ((), "")


class TestSupportCheck:
    def test_a_supported_core_in_a_complete_checkout_has_no_reasons(self, checkout):
        assert unsupported_reasons(generic_spec(str(PICORV32)), str(checkout)) == ()

    def test_files_missing_from_the_checkout_are_named(self, checkout):
        (checkout / TILE_DIR / "pico/rtl/picorv32.v").unlink()
        (reason,) = unsupported_reasons(generic_spec(str(PICORV32)), str(checkout))
        assert reason.endswith("pico/rtl/picorv32.v")

    def test_a_malformed_descriptor_is_a_reason(self, tmp_path):
        (reason,) = unsupported_reasons(generic_spec(descriptor_file(tmp_path, bus="axi")), None)
        assert "bus must be one of" in reason

    def test_a_missing_descriptor_file_is_a_reason(self, tmp_path):
        (reason,) = unsupported_reasons(generic_spec(str(tmp_path / "none.yaml")), None)
        assert "none.yaml" in reason


class TestRunsStopBeforeAnyBuild:
    """An Ariane-like core (RV64, write-back cache) and a SoC-like one (its
    own interconnect) end with status unsupported_core: FakeLLM has no
    replies, so any LLM call would raise, and no node is opened."""

    @pytest.fixture(autouse=True)
    def _no_nodes(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("a node was opened for an unsupported core")

        monkeypatch.setattr("mace.orchestrator.open_nodes", refuse)
        monkeypatch.setattr("mace.baselines.retry_agent.open_nodes", refuse)

    @pytest.mark.parametrize(
        ("override", "reason"),
        [
            ({"isa": {"march": "rv64imafdc", "mabi": "lp64d"}, "private_cache": "write_back"}, "RV64"),
            ({"own_interconnect": True}, "its own L2, L3, or NoC"),
        ],
    )
    def test_the_loop(self, tmp_path, checkout, override, reason):
        db = metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        spec = generic_spec(descriptor_file(tmp_path, **override))
        result = run_mace_loop((str(checkout),), spec, FakeLLM(responses=[]), db=db)
        assert result.status == "unsupported_core"
        assert result.iterations == ()
        assert any(reason in r for r in result.unsupported)
        assert runs(db) == ["unsupported_core"]
        assert not (checkout / FLIST_CORE).exists()

    def test_the_retry_agent(self, tmp_path, checkout):
        db = metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        spec = generic_spec(descriptor_file(tmp_path, own_interconnect=True))
        result = run_retry_agent(str(checkout), spec, FakeLLM(responses=[]), db)
        assert result.status == "unsupported_core"
        assert runs(db) == ["unsupported_core"]


class TestSupportedRun:
    def test_the_loop_writes_the_descriptor_s_files_before_its_first_build(self, tmp_path, checkout, monkeypatch):
        seen = []
        integrate = fake_integrate_parallel([step_result()])

        def integrate_after_prepare(piton_roots, *args, **kwargs):
            seen.append((checkout / FLIST_CORE).read_text())
            return integrate(piton_roots, *args, **kwargs)

        monkeypatch.setattr("mace.orchestrator.open_nodes", lambda piton_roots: [])
        monkeypatch.setattr("mace.orchestrator.check_plan", lambda *args, **kwargs: ())
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", integrate_after_prepare)
        db = metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        reply = "TASK: t1 | deps= | kind=workload | addi.S"
        result = run_mace_loop((str(checkout),), generic_spec(str(PICORV32)), FakeLLM(responses=[reply]), db=db)
        assert result.status == "passed"
        assert len(seen) == 1 and "../../pico/rtl/picorv32.v" in seen[0]
        assert "0x40000000" in (checkout / LINK_LD).read_text()


class TestUnwrittenWrapper:
    WRAPPER = "piton/design/chip/tile/generic/cores/picorv32/generic_core_wrapper.v"

    SKELETON = wrapper_skeleton(load_descriptor(str(PICORV32)))

    def test_the_skeleton_in_the_checkout_is_unwritten(self, checkout):
        (checkout / self.WRAPPER).write_text(self.SKELETON)
        assert unwritten_wrapper(generic_spec(str(PICORV32)), str(checkout)) == self.WRAPPER

    def test_a_written_wrapper_or_another_core_is_none(self, checkout):
        assert unwritten_wrapper(generic_spec(str(PICORV32)), str(checkout)) is None
        assert unwritten_wrapper(MaceSpec(workloads=("addi.S",), objective="x", core="pico"), str(checkout)) is None
        assert unwritten_wrapper(generic_spec(str(PICORV32)), None) is None

    def test_a_written_wrapper_that_keeps_the_marker_comments_is_written(self, checkout):
        # On the laptop, MACE's working SERV wrapper kept the "wrapper body" comments around its instance.
        written = self.SKELETON.replace(f"// end of {SKELETON_MARK}", f"picorv32 core (.clk(clk));\n    // end of {SKELETON_MARK}")
        assert written != self.SKELETON and SKELETON_MARK in written
        (checkout / self.WRAPPER).write_text(written)
        assert unwritten_wrapper(generic_spec(str(PICORV32)), str(checkout)) is None

    def test_carried_edits_decide_over_the_checkout(self, checkout):
        (checkout / self.WRAPPER).write_text(self.SKELETON)
        spec = generic_spec(str(PICORV32))
        assert unwritten_wrapper(spec, str(checkout), {self.WRAPPER: "picorv32 core (...);\n"}) is None
        assert unwritten_wrapper(spec, str(checkout), {self.WRAPPER: self.SKELETON}) == self.WRAPPER
        assert unwritten_wrapper(spec, str(checkout), {"piton/design/other.v": "x"}) == self.WRAPPER

    def test_the_loop_sends_a_plan_without_an_rtl_task_back(self, tmp_path, checkout, monkeypatch):
        (checkout / self.WRAPPER).write_text(self.SKELETON)
        monkeypatch.setattr("mace.orchestrator.open_nodes", lambda piton_roots: [])
        monkeypatch.setattr("mace.orchestrator.integrate_parallel", fake_integrate_parallel([step_result()]))
        db = metrics.open_db(str(tmp_path / "metrics.db"), ray_placement=False)
        llm = FakeLLM(responses=[
            "TASK: c1 | deps= | kind=config | Configure the 2x2 mesh",
            "TASK: r1 | deps= | kind=rtl | Write the wrapper body",
        ])
        spec = generic_spec(str(PICORV32))
        result = run_mace_loop((str(checkout),), spec, llm, db=db,
                               options=dataclasses.replace(LoopOptions(), rtl_edits=True))
        assert result.status == "passed"
        assert len(llm.calls) == 2
        assert "rejected before any build" in llm.calls[1][0] and "wrapper body' skeleton" in llm.calls[1][0]
        assert "rejected before any build" not in llm.calls[0][0]
        rows = db.query("SELECT action, problems FROM plan_checks WHERE run_id = ?", (result.run_id,))
        assert rows[0]["action"] == "rejected" and "wrapper_skeleton" in rows[0]["problems"]


def ran(first_requests, run_dir="/x/runs/1"):
    return PitonRunResult(
        success=True, returncode=0, test="addi.S", sim_type="vlt", run_dir=run_dir, verdict="pass",
        first_requests=first_requests,
    )


class TestBootGate:
    """A gate program can pass from the wrong start address, so a generic
    task passes only when every tile started at the reset vector."""

    def test_every_tile_at_the_reset_vector(self):
        spec = generic_spec(str(PICORV32))
        assert boot_failures(spec, (ran({0: 0x4000_0000, 1: 0x4000_0000}),), 2) == ()

    def test_a_tile_elsewhere_or_silent_is_a_failure(self):
        spec = generic_spec(str(PICORV32))
        problems = boot_failures(spec, (ran({0: 0x4000_0080}),), 2)
        assert len(problems) == 2
        assert "0x40000080" in problems[0] and problems[1].startswith("tile 1: the core made no memory request")

    def test_only_the_first_run_that_ran_counts(self):
        spec = generic_spec(str(PICORV32))
        assert boot_failures(spec, (ran({}, run_dir=""), ran({0: 0x4000_0000})), 1) == ()
        assert boot_failures(spec, (ran({}, run_dir=""),), 1) == ()

    def test_other_cores_are_not_checked(self):
        spec = MaceSpec(workloads=("addi.S",), objective="x", core="pico")
        assert boot_failures(spec, (ran({}),), 4) == ()

    def test_triage_sees_the_boot_problems(self):
        result = dataclasses.replace(step_result(), boot_problems=("tile 0: the core's first request was at x",))
        assert "Boot check" in failure_evidence(result) and "tile 0: the core's first request" in failure_evidence(result)


class TestPrompts:
    def test_every_method_s_inputs_name_the_core_and_its_wrapper(self):
        from mace.baselines.retry_agent import build_prompt as retry_prompt
        from mace.planner import build_prompt as planner_prompt
        from mace.planner import render_inputs

        spec = generic_spec(str(PICORV32))
        inputs = render_inputs(spec)
        assert "Core: picorv32 on OpenPiton's generic tile" in inputs
        assert "piton/design/chip/tile/generic/cores/picorv32/generic_core_wrapper.v" in inputs
        assert inputs in planner_prompt(spec) and inputs in retry_prompt(spec, [])

    def test_other_cores_get_no_core_lines(self):
        from mace.planner import render_inputs

        assert "Core:" not in render_inputs(MaceSpec(workloads=("addi.S",), objective="x", core="pico"))

    def test_the_rtl_agent_is_told_how_to_write_the_wrapper(self, tmp_path):
        from mace.integrator import rtl_prompt

        spec = generic_spec(descriptor_file(tmp_path, bus="obi", atomics="none",
                                            isa={"march": "rv32imc", "mabi": "ilp32"}))
        text = rtl_prompt(spec, Task(id="w", deps=(), kind="rtl", spec="write the wrapper"), {})
        assert "Replace its 'wrapper body' block" in text
        assert "generic_bridge_obi" in text
        assert "piton/design/chip/tile/generic/cores/picorv32/generic_core_wrapper.v" in text
        assert "0x40000000" in text and "bus: obi" in text

    def test_other_cores_rtl_prompts_have_no_wrapper_brief(self):
        from mace.integrator import rtl_prompt

        spec = MaceSpec(workloads=("addi.S",), objective="x", core="pico")
        assert "wrapper" not in rtl_prompt(spec, Task(id="w", deps=(), kind="rtl", spec="fix it"), {})
