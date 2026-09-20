"""Tier-0 tests for mace.agents' footer parsers.

Run:
    pytest mace/test/test_agents.py -q

The fixtures below are hand-written to look like real model output -- prose
around the directive lines, a model second-guessing itself -- not bare
one-line inputs, since the whole risk in a regex-over-prose parser is text
around the match, not the match itself.
"""

from __future__ import annotations

from pathlib import Path

from mace.agents import (
    KNOWN_ASSESSMENTS,
    KNOWN_DIAGNOSES,
    is_testbench_port_mismatch,
    parse_assessment,
    parse_cache_overrides,
    parse_diagnosis,
    parse_explanation,
    parse_fix,
    parse_next_steps,
    parse_tasks,
)
from mace.spec import Task

FIXTURES_DIR = Path(__file__).parent / "fixtures"

PLANNER_TRANSCRIPT = """\
Looking at the objective, I'll break this into three tasks: first configure
the 2x2 mesh, then run two gate workloads against it once the config lands.

TASK: cfg1 | deps= | kind=config | x_tiles=2,y_tiles=2,network_config=2dmesh_config
TASK: run_hello | deps=cfg1 | kind=workload | hello_world.c
TASK: run_accu | deps=cfg1 | kind=workload | accu_test.c

That should be enough to validate the mesh comes up correctly.
"""

TRIAGE_TRANSCRIPT_RECONSIDERED = """\
Looking at status.log, Cyc= is climbing steadily with no FAIL line, which at
first glance suggests a straightforward timeout.

DIAGNOSIS: timeout

Wait -- rtl_timeout was only 100000 for a 2x2 mesh, and OpenPiton's CI uses
10000000 for that mesh size. This isn't the RTL's fault, it's an
under-provisioned run.

DIAGNOSIS: config_error
FIX: rerun with -rtl_timeout=10000000, matching CI's convention for this mesh size
"""

POST_MORTEM_TRANSCRIPT_RECONSIDERED = """\
Every attempt reached the same point: generic boot completes, then the core
never reaches its own trap address. At first this looked like it might be
fixable with a longer rtl_timeout.

ASSESSMENT: fixable_config

Checked the logs again -- raising rtl_timeout to 10000000 changed nothing
except how long it took to hit the cap, which rules out "just needs more
cycles". The compiled binary's symbol table and entry point are both
verified correct.

ASSESSMENT: likely_hardware_limitation
EXPLANATION: the core never reaches its own trap address despite a verified-correct binary and generic boot completing identically to a known-good run
NEXT_STEPS: would need waveform-level tracing of the reset/boot sequence to go further
"""


class TestParseTasks:
    def test_extracts_every_task_from_prose(self):
        tasks = parse_tasks(PLANNER_TRANSCRIPT)
        assert tasks == (
            Task(
                id="cfg1",
                deps=(),
                kind="config",
                spec="x_tiles=2,y_tiles=2,network_config=2dmesh_config",
            ),
            Task(id="run_hello", deps=("cfg1",), kind="workload", spec="hello_world.c"),
            Task(id="run_accu", deps=("cfg1",), kind="workload", spec="accu_test.c"),
        )

    def test_no_task_lines_returns_empty(self):
        assert parse_tasks("I don't have enough information to plan yet.") == ()

    def test_tag_is_case_insensitive(self):
        tasks = parse_tasks("task: t1 | deps= | kind=config | x_tiles=1")
        assert tasks[0].id == "t1"

    def test_multiple_deps_split_on_comma(self):
        tasks = parse_tasks("TASK: t3 | deps=t1,t2 | kind=workload | accu_test.c")
        assert tasks[0].deps == ("t1", "t2")

    def test_whitespace_around_fields_is_stripped(self):
        tasks = parse_tasks("TASK:  t1  |  deps=  |  kind=config  |  x_tiles=1  ")
        assert (tasks[0].id, tasks[0].deps, tasks[0].kind, tasks[0].spec) == (
            "t1",
            (),
            "config",
            "x_tiles=1",
        )

    def test_malformed_line_is_skipped_not_raised(self):
        text = (
            "TASK: t1 | deps= | kind=config | x_tiles=1\n"
            "TASK: this one is missing fields\n"
            "TASK: t2 | deps=t1 | kind=workload | hello_world.c\n"
        )
        tasks = parse_tasks(text)
        assert [t.id for t in tasks] == ["t1", "t2"]

    def test_blank_task_line_does_not_swallow_the_next_task_line(self):
        """A TASK line left blank must not consume the next TASK line --
        including its own tag -- as its spec field.
        """
        text = "TASK:\nTASK: t2 | deps= | kind=workload | hello_world.c\n"
        tasks = parse_tasks(text)
        assert [t.id for t in tasks] == ["t2"]

    def test_unknown_kind_is_skipped(self):
        text = "TASK: t1 | deps= | kind=rebuild | x_tiles=1\n"
        assert parse_tasks(text) == ()

    def test_blank_id_is_skipped(self):
        text = "TASK:  | deps= | kind=config | x_tiles=1\n"
        assert parse_tasks(text) == ()

    def test_task_named_in_a_caches_line_gets_the_override(self):
        text = (
            "TASK: t1 | deps= | kind=config | build with a tiny L1D\n"
            "CACHES: t1 | l1d=128,1\n"
        )
        tasks = parse_tasks(text)
        assert tasks[0].caches == (("l1d", (128, 1)),)

    def test_task_not_named_in_any_caches_line_keeps_none(self):
        text = (
            "TASK: t1 | deps= | kind=config | build with a tiny L1D\n"
            "CACHES: t2 | l1d=128,1\n"
        )
        tasks = parse_tasks(text)
        assert tasks[0].caches is None


class TestParseCacheOverrides:
    def test_single_cache_for_one_task(self):
        overrides = parse_cache_overrides("CACHES: t1 | l1d=128,1\n")
        assert overrides == {"t1": (("l1d", (128, 1)),)}

    def test_multiple_caches_on_one_line(self):
        overrides = parse_cache_overrides("CACHES: t1 | l1d=128,1 l1i=16384,4\n")
        assert overrides == {"t1": (("l1d", (128, 1)), ("l1i", (16384, 4)))}

    def test_different_tasks_get_independent_overrides(self):
        text = "CACHES: t1 | l1d=128,1\nCACHES: t4 | l1d=8192,4\n"
        overrides = parse_cache_overrides(text)
        assert overrides == {"t1": (("l1d", (128, 1)),), "t4": (("l1d", (8192, 4)),)}

    def test_later_line_for_the_same_task_merges_in(self):
        text = "CACHES: t1 | l1d=128,1\nCACHES: t1 | l1i=16384,4\n"
        overrides = parse_cache_overrides(text)
        assert overrides == {"t1": (("l1d", (128, 1)), ("l1i", (16384, 4)))}

    def test_later_line_for_the_same_task_and_cache_wins(self):
        text = "CACHES: t1 | l1d=128,1\nCACHES: t1 | l1d=256,2\n"
        overrides = parse_cache_overrides(text)
        assert overrides == {"t1": (("l1d", (256, 2)),)}

    def test_unknown_cache_name_is_dropped_not_the_whole_line(self):
        overrides = parse_cache_overrides("CACHES: t1 | l3=128,1 l1d=8192,4\n")
        assert overrides == {"t1": (("l1d", (8192, 4)),)}

    def test_non_integer_geometry_is_dropped(self):
        overrides = parse_cache_overrides("CACHES: t1 | l1d=big,1\n")
        assert overrides == {}

    def test_non_positive_geometry_is_dropped(self):
        overrides = parse_cache_overrides("CACHES: t1 | l1d=0,1\n")
        assert overrides == {}

    def test_missing_pipe_is_dropped(self):
        assert parse_cache_overrides("CACHES: t1 l1d=128,1\n") == {}

    def test_blank_task_id_is_dropped(self):
        assert parse_cache_overrides("CACHES:  | l1d=128,1\n") == {}

    def test_no_caches_lines_returns_empty(self):
        assert parse_cache_overrides("TASK: t1 | deps= | kind=config | x\n") == {}


class TestParseDiagnosis:
    def test_last_diagnosis_wins_over_a_reconsidered_first_guess(self):
        assert parse_diagnosis(TRIAGE_TRANSCRIPT_RECONSIDERED) == "config_error"

    def test_value_is_lowercased(self):
        assert parse_diagnosis("DIAGNOSIS: Timeout") == "timeout"

    def test_no_diagnosis_returns_none(self):
        assert parse_diagnosis("Still investigating, no conclusion yet.") is None

    def test_blank_tag_line_does_not_swallow_the_next_line(self):
        """A tag left blank on its own line must not consume the next
        line -- including that line's own tag -- as its value.
        """
        assert parse_diagnosis("DIAGNOSIS:\nFIX: raise rtl_timeout") is None

    def test_known_diagnoses_are_not_enforced_by_the_parser(self):
        """Taxonomy membership is the caller's call, not the parser's -- see
        parse_diagnosis's docstring. An unrecognized value still comes back.
        """
        assert parse_diagnosis("DIAGNOSIS: mystery_bug") == "mystery_bug"
        assert "mystery_bug" not in KNOWN_DIAGNOSES

    def test_every_documented_taxonomy_value_is_in_known_diagnoses(self):
        for value in (
            "test_bug", "config_error", "timeout", "maxcycles", "rtl_suspect", "testbench_mismatch",
        ):
            assert value in KNOWN_DIAGNOSES


class TestParseFix:
    def test_last_fix_wins(self):
        assert parse_fix(TRIAGE_TRANSCRIPT_RECONSIDERED) == (
            "rerun with -rtl_timeout=10000000, matching CI's convention for this mesh size"
        )

    def test_no_fix_returns_none(self):
        assert parse_fix("DIAGNOSIS: timeout\n") is None

    def test_fix_text_is_not_lowercased(self):
        assert parse_fix("FIX: Rerun with -Verbose") == "Rerun with -Verbose"


class TestParseAssessment:
    def test_last_assessment_wins_over_a_reconsidered_first_guess(self):
        assert parse_assessment(POST_MORTEM_TRANSCRIPT_RECONSIDERED) == "likely_hardware_limitation"

    def test_value_is_lowercased(self):
        assert parse_assessment("ASSESSMENT: Fixable_Config") == "fixable_config"

    def test_no_assessment_returns_none(self):
        assert parse_assessment("Still investigating, no conclusion yet.") is None

    def test_known_assessments_are_not_enforced_by_the_parser(self):
        assert parse_assessment("ASSESSMENT: mystery_verdict") == "mystery_verdict"
        assert "mystery_verdict" not in KNOWN_ASSESSMENTS

    def test_every_documented_taxonomy_value_is_in_known_assessments(self):
        for value in ("fixable_config", "likely_hardware_limitation", "inconclusive"):
            assert value in KNOWN_ASSESSMENTS


class TestParseExplanation:
    def test_last_explanation_wins(self):
        assert parse_explanation(POST_MORTEM_TRANSCRIPT_RECONSIDERED) == (
            "the core never reaches its own trap address despite a "
            "verified-correct binary and generic boot completing "
            "identically to a known-good run"
        )

    def test_no_explanation_returns_none(self):
        assert parse_explanation("ASSESSMENT: inconclusive\n") is None


class TestParseNextSteps:
    def test_last_next_steps_wins(self):
        assert parse_next_steps(POST_MORTEM_TRANSCRIPT_RECONSIDERED) == (
            "would need waveform-level tracing of the reset/boot sequence to go further"
        )

    def test_no_next_steps_returns_none(self):
        assert parse_next_steps("ASSESSMENT: inconclusive\n") is None

    def test_next_steps_text_is_not_lowercased(self):
        assert parse_next_steps("NEXT_STEPS: Try a Larger Mesh") == "Try a Larger Mesh"


class TestIsTestbenchPortMismatch:
    """Fixture is a real capture, not a guess: taken by deliberately
    renaming a working port connection in the proven pico_reset_ut_top.v
    testbench and rebuilding against real Verilator -- see
    mace.agents.is_testbench_port_mismatch's own comment for how."""

    def test_real_pinnotfound_capture_is_detected(self):
        text = (FIXTURES_DIR / "build_fail_pinnotfound.log").read_text()
        assert is_testbench_port_mismatch(text) is True

    def test_unrelated_build_failure_is_not_a_mismatch(self):
        assert is_testbench_port_mismatch("%Error: Exiting due to 1 error(s)\n") is False

    def test_pinmissing_alone_is_not_a_mismatch(self):
        """An unconnected DUT pin is a separate, non-fatal warning -- often
        intentional (see mace.loop._run_unit_test_step's own tie-offs), not
        evidence the testbench named a nonexistent port."""
        text = "%Warning-PINMISSING: foo.v:1:1: Cell has missing pin: 'bar'\n"
        assert is_testbench_port_mismatch(text) is False

    def test_empty_stderr_is_not_a_mismatch(self):
        assert is_testbench_port_mismatch("") is False
