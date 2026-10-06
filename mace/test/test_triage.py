"""Tier-0 tests for mace.triage.

Run:
    pytest mace/test/test_triage.py -q
"""

from __future__ import annotations

import pytest

from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult
from mace.spec import StepResult, Task
from mace.test.conftest import FakeLLM
from mace.triage import TriageError, build_prompt, triage


def make_failed_result(build_success=True, verdict="fail"):
    cfg = PitonConfig()
    build = PitonBuildArtifact(
        success=build_success, returncode=0 if build_success else 1, config=cfg,
        sim_type="vlt", model_dir="/x", binary_path="/x/Vcmp_top" if build_success else "",
        wall_time_s=1.0, failure_reason="" if build_success else "verilator_bad_option",
        stderr="" if build_success else "%Error: bad flag",
    )
    run = None
    if build_success:
        run = PitonRunResult(
            success=False, returncode=0, test="hello_world.c", sim_type="vlt",
            run_dir="/x/runs/1", verdict=verdict,
            sim_log_tail="1234 : Simulation -> FAIL(TIMEOUT)",
            status_log="Diag: hello_world.c Timeout (TIMEOUT)",
        )
    task = Task(id="t1", deps=(), kind="workload", spec="hello_world.c")
    query = FakeLLM(responses=["edit"]).prompt("edit")
    return StepResult(task=task, query=query, build=build, run=run, passed=False)


class TestBuildPrompt:
    def test_includes_task_and_verdict(self):
        prompt = build_prompt(make_failed_result(verdict="timeout"))
        assert "t1" in prompt
        assert "hello_world.c" in prompt
        assert "timeout" in prompt

    def test_build_failure_includes_stderr_not_run_log(self):
        prompt = build_prompt(make_failed_result(build_success=False))
        assert "verilator_bad_option" in prompt
        assert "%Error: bad flag" in prompt

    def test_names_the_build_flags(self):
        """The defines and cache sizes let triage tie a failure to the plan."""
        prompt = build_prompt(make_failed_result())
        assert "-config_rtl=MINIMAL_MONITORING" in prompt
        assert "-config_l15_size=8192" in prompt

    def test_build_failure_shows_error_lines_and_the_stdout_tail(self):
        """sims prints Verilator's diagnostics to stdout and leaves stderr empty."""
        failed = make_failed_result(build_success=False)
        error = "%Error-PINNOTFOUND: manycore_top.tmp.v:361:6: Pin not found: 'async_mux'"
        failed.build.stderr = ""
        failed.build.stdout = error + "\n%Warning-WIDTH: x.v:1:1: w\n" * 3
        failed.build.errors = (error,)
        prompt = build_prompt(failed)
        assert f"Build errors:\n{error}" in prompt
        assert "%Warning-WIDTH" in prompt  # the stdout tail


class TestTriage:
    def test_returns_parsed_diagnosis_and_fix(self):
        llm = FakeLLM(responses=["DIAGNOSIS: timeout\nFIX: raise rtl_timeout\n"])
        result = triage(make_failed_result(), llm)
        assert (result.diagnosis, result.fix) == ("timeout", "raise rtl_timeout")

    def test_dispatches_with_the_built_prompt_and_tools(self):
        llm = FakeLLM(responses=["DIAGNOSIS: timeout\nFIX: x\n"])
        sentinel_tool = object()
        failed = make_failed_result()

        triage(failed, llm, tools=[sentinel_tool])

        message, tools = llm.calls[0]
        assert message == build_prompt(failed)
        assert tools == (sentinel_tool,)

    def test_missing_fix_defaults_to_empty_string(self):
        llm = FakeLLM(responses=["DIAGNOSIS: timeout\n"])
        result = triage(make_failed_result(), llm)
        assert result.fix == ""

    def test_no_diagnosis_line_raises(self):
        llm = FakeLLM(responses=["I'm not sure what happened."])
        with pytest.raises(TriageError, match="no DIAGNOSIS:"):
            triage(make_failed_result(), llm)


class TestTestbenchMismatchShortCircuit:
    """A real PINNOTFOUND build failure is diagnosed mechanically -- no LLM
    call needed (see mace.triage.triage's own docstring for why)."""

    def _pinnotfound_result(self):
        cfg = PitonConfig()
        build = PitonBuildArtifact(
            success=False, returncode=1, config=cfg, sim_type="vlt", model_dir="/x",
            binary_path="", wall_time_s=1.0, failure_reason="verilator_error",
            stderr="%Error-PINNOTFOUND: foo_ut_top.v:56:10: Pin not found: 'mem_valid_WRONG'\n",
        )
        task = Task(id="t1", deps=(), kind="unit_test", spec="picorv32.v")
        query = FakeLLM(responses=["edit"]).prompt("edit")
        return StepResult(task=task, query=query, build=build, run=None, passed=False)

    def test_diagnoses_without_calling_the_llm(self):
        llm = FakeLLM(responses=[])  # would raise if prompt() were called
        result = triage(self._pinnotfound_result(), llm)
        assert result.diagnosis == "testbench_mismatch"
        assert "t1" in result.fix

    def test_finds_the_signature_in_stdout(self):
        """Where sims leaves it: Verilator's output goes to stdout."""
        result = self._pinnotfound_result()
        result.build.stdout, result.build.stderr = result.build.stderr, ""
        diagnosed = triage(result, FakeLLM(responses=[]))
        assert diagnosed.diagnosis == "testbench_mismatch"

    def test_other_build_failures_still_go_through_the_llm(self):
        llm = FakeLLM(responses=["DIAGNOSIS: rtl_suspect\nFIX: investigate\n"])
        result = triage(make_failed_result(build_success=False), llm)
        assert result.diagnosis == "rtl_suspect"
        assert len(llm.calls) == 1

    def test_same_stderr_on_a_non_unit_test_task_still_goes_through_the_llm(self):
        """%Error-PINNOTFOUND is a generic Verilator error, not unique to a
        scaffolded unit-test testbench -- a config/workload task hitting the
        same signature is a real RTL regression, not a testbench mismatch,
        and there's no scaffolded testbench to blame it on.
        """
        result = self._pinnotfound_result()
        result = StepResult(
            task=Task(id=result.task.id, deps=(), kind="config", spec=result.task.spec),
            query=result.query, build=result.build, run=result.run, passed=result.passed,
        )
        llm = FakeLLM(responses=["DIAGNOSIS: rtl_suspect\nFIX: investigate\n"])
        diagnosed = triage(result, llm)
        assert diagnosed.diagnosis == "rtl_suspect"
        assert len(llm.calls) == 1


class TestChangesFromDefaults:
    def test_a_default_config_changes_nothing(self):
        from chia_openpiton.state_def import PitonConfig
        from mace.triage import changes_from_defaults

        assert changes_from_defaults(PitonConfig()) == "none"

    def test_names_added_defines_changed_caches_and_the_crossbar(self):
        from chia_openpiton.state_def import DEFAULT_CACHES, PitonConfig
        from mace.triage import changes_from_defaults

        config = PitonConfig(
            config_rtl=("MINIMAL_MONITORING", "PITON_FPGA_SYNTH"),
            caches={**DEFAULT_CACHES, "l1d": (4096, 2)},
            network_config="xbar_config",
        )
        assert changes_from_defaults(config) == (
            "RTL defines added: PITON_FPGA_SYNTH; l1d=4096,2 (default 8192,4); network xbar_config"
        )

    def test_the_prompt_carries_the_changes_line(self):
        from chia_openpiton.state_def import PitonConfig
        from mace.triage import build_prompt

        result = make_failed_result(build_success=False)
        result.build.config = PitonConfig(config_rtl=("MINIMAL_MONITORING", "PITON_FPGA_SYNTH"))
        assert "Changes from the defaults: RTL defines added: PITON_FPGA_SYNTH" in build_prompt(result)


class TestObjectiveInTheprompt:
    def test_the_objective_leads_the_prompt_when_given(self):
        from mace.triage import build_prompt

        prompt = build_prompt(make_failed_result(), objective="Bring up pico; it needs CONFIG_DISABLE_BIST_CLEAR.")
        assert prompt.startswith("Run objective: Bring up pico; it needs CONFIG_DISABLE_BIST_CLEAR.\n\nTask t1")

    def test_without_an_objective_the_prompt_starts_with_the_task(self):
        from mace.triage import build_prompt

        assert build_prompt(make_failed_result()).startswith("Task t1")

    def test_triage_passes_the_objective_to_the_llm(self):
        from mace.triage import triage

        llm = FakeLLM(responses=["DIAGNOSIS: config_error\nFIX: add the define"])
        triage(make_failed_result(), llm, objective="needs CONFIG_DISABLE_BIST_CLEAR")
        assert "Run objective: needs CONFIG_DISABLE_BIST_CLEAR" in llm.calls[0][0]


class TestRtlEditsInTheEvidence:
    def test_the_evidence_and_the_feedback_quote_the_edits_diff(self):
        from mace.triage import edits_feedback, failure_evidence

        result = make_failed_result()
        result.edits_diff = "--- a/piton/design/x.v\n+++ b/piton/design/x.v\n-old\n+new\n"

        assert "RTL edits the build carried:\n--- a/piton/design/x.v" in failure_evidence(result)
        assert edits_feedback(result).startswith("\nThe RTL edits it was built with:\n--- a/piton/design/x.v")

    def test_no_edits_add_nothing(self):
        from mace.triage import edits_feedback, failure_evidence

        result = make_failed_result()
        assert "RTL edits" not in failure_evidence(result)
        assert edits_feedback(result) == ""


class TestAnRtlTaskThatRecordedNoEdit:
    def test_the_evidence_says_so_and_quotes_what_the_agent_said(self):
        from mace.triage import failure_evidence

        result = make_failed_result()
        result.edit_recorded = False
        result.query = FakeLLM(responses=["I could not find piton/design/pico_mesh.v"]).prompt("x")

        text = failure_evidence(result)

        assert "recorded no edit, so the design was built as it stood" in text
        assert "I could not find piton/design/pico_mesh.v" in text

    def test_a_task_that_recorded_one_adds_no_such_line(self):
        from mace.triage import failure_evidence

        assert "recorded no edit" not in failure_evidence(make_failed_result())
