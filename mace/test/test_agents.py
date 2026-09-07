"""Tier-0 tests for mace.agents' footer parsers.

Run:
    pytest mace/test/test_agents.py -q

The fixtures below are hand-written to look like real model output -- prose
around the directive lines, a model second-guessing itself -- not bare
one-line inputs, since the whole risk in a regex-over-prose parser is text
around the match, not the match itself.
"""

from __future__ import annotations

from mace.agents import KNOWN_DIAGNOSES, parse_diagnosis, parse_fix, parse_tasks
from mace.spec import Task

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

    def test_unknown_kind_is_skipped(self):
        text = "TASK: t1 | deps= | kind=rebuild | x_tiles=1\n"
        assert parse_tasks(text) == ()

    def test_blank_id_is_skipped(self):
        text = "TASK:  | deps= | kind=config | x_tiles=1\n"
        assert parse_tasks(text) == ()


class TestParseDiagnosis:
    def test_last_diagnosis_wins_over_a_reconsidered_first_guess(self):
        assert parse_diagnosis(TRIAGE_TRANSCRIPT_RECONSIDERED) == "config_error"

    def test_value_is_lowercased(self):
        assert parse_diagnosis("DIAGNOSIS: Timeout") == "timeout"

    def test_no_diagnosis_returns_none(self):
        assert parse_diagnosis("Still investigating, no conclusion yet.") is None

    def test_known_diagnoses_are_not_enforced_by_the_parser(self):
        """Taxonomy membership is the caller's call, not the parser's -- see
        parse_diagnosis's docstring. An unrecognized value still comes back.
        """
        assert parse_diagnosis("DIAGNOSIS: mystery_bug") == "mystery_bug"
        assert "mystery_bug" not in KNOWN_DIAGNOSES

    def test_every_documented_taxonomy_value_is_in_known_diagnoses(self):
        for value in ("test_bug", "config_error", "timeout", "maxcycles", "rtl_suspect"):
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
