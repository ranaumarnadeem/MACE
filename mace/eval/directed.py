"""mace.eval.directed -- the evaluation of directed tests (session B).

A directed test written by an agent counts only if it passes on the correct
design and fails on the planted bug in the logic it targets. This module
checks that with the harness' own source-fault machinery (see
:mod:`mace.eval.source_faults`):

* :func:`check_tests` runs every test a run wrote on the task's expert
  design twice, with the planted bug and without, and classifies each test
  (:func:`classify`). It calls no LLM and runs after the method has
  finished, outside the method's budget.
* :func:`run_testgen` is the detection method: the expert design with the
  bug, its failing gate workload as evidence, and one ``test`` task that
  writes tests for it. Its run ends ``passed`` when a test was detected.
* :func:`test_first` is the plan hook of the repair method
  ``mace_rtl_test``: it puts a ``test`` task before each ``rtl`` task, so the
  repair agent is handed the tests and their verdicts on the unrepaired design.

The directed tests themselves are written and run by :mod:`mace.directed`.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from pathlib import Path

from chia.database.sqlite_node import SQLiteNode

from mace import directed, usage
from mace.integrator import close_nodes, integrate_parallel, open_nodes
from mace.metrics import (
    RunLabels,
    finish_run,
    record_iteration,
    record_llm_calls,
    record_test_check,
    start_run,
)
from mace.spec import DirectedTest, LoopOptions, LoopResult, MaceSpec, StepResult, Task
from mace.triage import failure_evidence
from mace.workloads import verify_checksums

logger = logging.getLogger(__name__)

# The outcomes of a test's check, in the order a report lists them.
DETECTED = "detected"  # passes on the correct design, fails on the bug
MISSED = "missed"  # passes on both
FALSE_ALARM = "false_alarm"  # fails on the correct design
INVALID = "invalid"  # gave no verdict where it had to
UNCHECKED = "unchecked"  # the bug breaks the build, so no test can fail on it
RUNTIME_BROKEN = "runtime_broken"  # the bug fails every program, the null test included
OUTCOMES = (DETECTED, MISSED, FALSE_ALARM, INVALID, UNCHECKED, RUNTIME_BROKEN)

# Verdicts of a test that ran: it passed, or failed in one of three ways.
FAILING = frozenset(("fail", "timeout", "maxcycles"))
TESTGEN_TASK_ID = "test"

# The null test: a program that checks nothing. Run on both designs next to the
# generated tests, it shows whether a failure on the bug means anything. A bug
# that fails even this program, such as an atomic-add bug that hangs the C
# runtime's exit barrier, fails every test, so no test has found that bug.
NULL_NAME = "null"
NULL_KEY = (-1, NULL_NAME, 0, NULL_NAME)
NULL_SOURCES = {
    "S": (
        '#include "riscv_test.h"\n#include "test_macros.h"\nRVTEST_RV32U\nRVTEST_CODE_BEGIN\n'
        "  TEST_CASE( 2, x2, 1, li x2, 1; )\n  TEST_PASSFAIL\nRVTEST_CODE_END\n  .data\nRVTEST_DATA_BEGIN\n"
        "  TEST_DATA\nRVTEST_DATA_END\n"
    ),
    "c": "#include <stdio.h>\n\nint main(int argc, char **argv) {\n  return 0;\n}\n",
}


def classify(
    fault_verdict: str, clean_verdict: str, null_fault_verdict: str = "pass", null_clean_verdict: str = "pass"
) -> str:
    """A test's outcome from its verdict on the design with the bug and on
    the design without it, and the null test's verdicts on the two. A hang
    (``timeout``, ``maxcycles``) is a failure. A failure on the bug counts as
    a detection only if the null test still passes there."""
    if null_clean_verdict != "pass":
        return INVALID  # not even a program that checks nothing passes on the correct design
    if clean_verdict in FAILING:
        return FALSE_ALARM
    if clean_verdict != "pass":
        return INVALID
    if fault_verdict == "not_built":
        return UNCHECKED
    if fault_verdict in FAILING:
        return DETECTED if null_fault_verdict == "pass" else RUNTIME_BROKEN
    if fault_verdict == "pass":
        return MISSED
    return INVALID


@dataclasses.dataclass(frozen=True)
class TestCheck:
    """One test's mutation check."""

    # Not a pytest test class, though the name starts with "Test".
    __test__ = False

    iteration: int
    task_id: str
    attempt: int
    name: str
    fault_verdict: str
    clean_verdict: str
    outcome: str
    wall_s: float = 0.0


def test_first() -> Callable[[int, tuple[Task, ...]], tuple[Task, ...]]:
    """A ``plan_hook`` for :func:`mace.orchestrator.run_mace_loop` that puts a
    ``test`` task before every ``rtl`` task in every plan.

    The test task has the rtl task's dependencies and build overrides, so it
    tests the design the rtl task will change; the rtl task then depends on
    it and inherits its tests.
    """

    def hook(iteration: int, tasks: tuple[Task, ...]) -> tuple[Task, ...]:
        taken = {t.id for t in tasks}
        planned: list[Task] = []
        for t in tasks:
            if t.kind != "rtl":
                planned.append(t)
                continue
            test_id = f"test_{t.id}"
            while test_id in taken:
                test_id += "_"
            taken.add(test_id)
            planned.append(
                Task(
                    id=test_id, deps=t.deps, kind="test",
                    spec=f"Write directed tests for the logic that task {t.id} changes: {t.spec}",
                    caches=t.caches, config_rtl=t.config_rtl, network=t.network,
                )
            )
            planned.append(dataclasses.replace(t, deps=(*t.deps, test_id)))
        return tuple(planned)

    return hook


def tests_of_run(db: SQLiteNode, run_id: str) -> list[tuple[tuple[int, str, int, str], DirectedTest]]:
    """Every test *run_id* wrote, as ((iteration, task, attempt, name), test)."""
    rows = db.query(
        "SELECT iteration, task_id, attempt, name, target, form, source, verdict, wall_s, evidence "
        "FROM directed_tests WHERE run_id = ? ORDER BY iteration, task_id, attempt, name",
        (run_id,),
    )
    return [
        (
            (r["iteration"], r["task_id"], r["attempt"], r["name"]),
            DirectedTest(
                r["name"], r["target"], r["form"], r["source"], attempt=r["attempt"], verdict=r["verdict"],
                wall_s=r["wall_s"], evidence=r["evidence"] or "",
            ),
        )
        for r in rows
    ]


def _stage(root: str, tests) -> list[tuple[tuple[int, str, int, str], DirectedTest, str]]:
    """Write each test into a directory of its own under the checkout's test
    area (sims stops at two diags of one name in a directory), and return
    (key, test, directory) for each."""
    base = Path(root) / directed.TEST_DIR / "check"
    staged = []
    for i, (key, test) in enumerate(tests):
        directory = base / str(i)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / test.program).write_text(test.source, encoding="utf-8", newline="\n")
        staged.append((key, test, str(directory)))
    return staged


def _verdict_of(result: StepResult) -> str:
    if not result.build.success:
        return "not_built"
    return directed.verdict_of(result.runs[-1]) if result.runs else "no_verdict"


def _run_on_design(roots, spec: MaceSpec, expert: Task, staged, nodes) -> dict[tuple, tuple[str, float]]:
    """Each staged test's verdict (and seconds) on the expert design as the
    checkout now stands. The design is built once; later calls reuse it."""
    verdicts = {}
    for key, test, directory in staged:
        started = time.monotonic()
        one = dataclasses.replace(spec, workloads=(test.program,), workload_args=())
        results = integrate_parallel(
            roots, one, (expert,), None, asm_diag_root=directory, nodes=nodes,
            options=LoopOptions(task_prompts=False),
        )
        verdicts[key] = (_verdict_of(results[0]), time.monotonic() - started)
    return verdicts


def check_tests(
    roots: tuple[str, ...], spec: MaceSpec, expert: Task, db: SQLiteNode, run_id: str
) -> list[TestCheck]:
    """The mutation check of every directed test *run_id* wrote.

    Call it inside the job's ``faulted`` block: the checkout then carries
    the task's bug. Each test runs on the expert design with the bug, then
    the bug is restored and each runs on the correct design. Records one
    ``test_checks`` row per test and returns them. The first of *roots* is
    used.
    """
    from mace.eval.runner import clear_build_cache
    from mace.eval.source_faults import restore

    tests = tests_of_run(db, run_id)
    if not tests:
        return []
    roots = roots[:1]
    null = DirectedTest(NULL_NAME, "", tests[0][1].form, NULL_SOURCES[tests[0][1].form])
    staged = _stage(roots[0], [(NULL_KEY, null), *tests])
    nodes = open_nodes(roots)
    try:
        with_bug = _run_on_design(roots, spec, expert, staged, nodes)
        # The correct design has its own source fingerprint, so it rebuilds;
        # an empty cache keeps the two builds apart all the same.
        restore(roots[0])
        clear_build_cache(roots[0])
        clean = _run_on_design(roots, spec, expert, staged, nodes)
    finally:
        close_nodes(nodes)
    (null_fault, null_fault_s), (null_clean, null_clean_s) = with_bug[NULL_KEY], clean[NULL_KEY]
    iteration, task_id, attempt, name = NULL_KEY
    record_test_check(
        db, run_id, iteration, task_id, attempt, name, null_fault, null_clean, "null", null_fault_s + null_clean_s
    )
    checks = []
    for key, _test, _directory in staged:
        if key == NULL_KEY:
            continue
        (fault_verdict, fault_s), (clean_verdict, clean_s) = with_bug[key], clean[key]
        iteration, task_id, attempt, name = key
        check = TestCheck(
            iteration, task_id, attempt, name, fault_verdict, clean_verdict,
            classify(fault_verdict, clean_verdict, null_fault, null_clean), fault_s + clean_s,
        )
        record_test_check(
            db, run_id, iteration, task_id, attempt, name, fault_verdict, clean_verdict, check.outcome, check.wall_s
        )
        checks.append(check)
    return checks


def run_testgen(
    piton_roots: tuple[str, ...],
    spec: MaceSpec,
    expert: Task,
    llm,
    db: SQLiteNode,
    labels: RunLabels | None = None,
    tools=(),
) -> LoopResult:
    """The detection method: write directed tests for a planted bug.

    Iteration 0 builds the expert design (which carries the bug) and runs the
    gate workloads. Iteration 1 is one ``test`` task on the same design, told
    what the gate workloads reported. The run ends ``passed`` when the mutation
    check finds a test that passes on the correct design and fails on the bug,
    and ``failed`` otherwise; the check itself is recorded in ``test_checks``.
    Uses the first checkout.
    """
    run_id = start_run(db, spec, labels=labels or RunLabels(method="testgen"))
    try:
        try:
            verify_checksums()
        except ValueError:
            finish_run(db, run_id, "checksum_mismatch")
            return LoopResult(run_id=run_id, status="checksum_mismatch", iterations=())
        roots = piton_roots[:1]
        started = time.monotonic()
        deadline = started + spec.budget.max_wall_s
        nodes = open_nodes(roots)
        iterations: list[tuple[StepResult, ...]] = []
        try:
            first = integrate_parallel(
                roots, spec, (expert,), None, run_id=run_id, iteration=0, nodes=nodes,
                options=LoopOptions(task_prompts=False), deadline=deadline,
            )
            record_iteration(db, run_id, 0, first, time.monotonic() - started)
            iterations.append(first)
            report = (
                "The gate workloads fail on this design:\n" + failure_evidence(first[0])
                if not first[0].passed
                else "The gate workloads pass on this design, so the bug is not one they expose."
            )
            task = Task(
                id=TESTGEN_TASK_ID, deps=(), kind="test",
                spec=(
                    "This design has a bug under piton/design/. Write directed tests for the logic you "
                    "suspect is wrong: each must fail while the bug is there and pass on a correct design."
                ),
                caches=expert.caches, config_rtl=expert.config_rtl, network=expert.network,
            )
            log = usage.UsageLog()
            second_started = time.monotonic()
            with usage.recording(log):
                second = integrate_parallel(
                    roots, spec, (task,), llm, tools=tools, run_id=run_id, iteration=1, nodes=nodes,
                    options=LoopOptions(task_prompts=False), deadline=deadline, rtl_context=report,
                )
            record_iteration(
                db, run_id, 1, second, time.monotonic() - second_started, usd=sum(c.usd for c in log.calls())
            )
            record_llm_calls(db, run_id, 1, log.take())
            iterations.append(second)
        finally:
            close_nodes(nodes)
        checks = check_tests(roots, spec, expert, db, run_id)
        status = "passed" if any(c.outcome == DETECTED for c in checks) else "failed"
        finish_run(db, run_id, status)
        return LoopResult(run_id=run_id, status=status, iterations=tuple(iterations))
    except BaseException:
        finish_run(db, run_id, "error")
        raise


