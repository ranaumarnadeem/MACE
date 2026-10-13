"""mace.directed -- directed tests: an agent writes test programs for the logic
it suspects, and the loop runs them.

A ``test`` task (kind ``test``, see :mod:`mace.spec`) builds the design as it
stands and asks an agent for up to :data:`mace.tools.MAX_TESTS_PER_TASK` test
programs: RV32 assembly for PicoRV32, C for Ariane. The agent writes them
through a :class:`~mace.tools.DirectedTestTool`, which saves them as
``dt_<name>.<form>`` in a directory of their own under the checkout's
``build/.mace_tests/``. The loop runs each test on every tile of the built
design and records the verdict. A test that does not compile goes back to the
agent for up to :data:`MAX_TEST_ATTEMPTS` attempts in all.

A test adds checks and never replaces the gate workloads. The tasks that
depend on a ``test`` task run its tests after their gate workloads, and pass
only if both pass. The tests never go in ``mace/workloads/``, whose files are
checksummed.

Nothing here decides whether a test is good. That check needs the planted
bug, and it lives in :mod:`mace.eval.directed`: a test counts when it passes
on the correct design and fails on the bug.

The cluster calls (reading the tests back, running one) are arguments of
:func:`run_test_attempts`, so a tier-0 test needs neither Ray nor OpenPiton.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Callable

import ray
from chia.base.ChiaFunction import get
from chia.base.llm_call import QueryResult
from chia_openpiton.state_def import PitonRunResult
from mace import usage
from mace.spec import DirectedTest, MaceSpec, Task
from mace.tools import MAX_TESTS_PER_TASK, TEST_SIDECAR, DirectedTestTool

logger = logging.getLogger(__name__)

# Where a checkout keeps the tests of a run; the build cache clear of
# mace.eval.runner empties it before every evaluation job.
TEST_DIR = os.path.join("build", ".mace_tests")
MAX_TEST_ATTEMPTS = 3
# How much of a log a test's record and a prompt quote.
EVIDENCE_CHARS = 1200
PROMPT_EVIDENCE_CHARS = 600
PROMPT_SOURCE_CHARS = 1500

# The test form each core takes: PicoRV32 runs RV32 assembly, Ariane C.
FORM_BY_CORE = {"pico": "S", "ariane": "c"}
# Verdicts of a test that did not run to a verdict, so it cannot gate anything.
UNUSABLE = frozenset(("no_verdict", "not_built"))

_ASM_STYLE = """\
A directed test is a short RV32 assembly program that checks one piece of logic in the design. Every tile runs the
same program on shared memory, and the run passes only when every hart reaches the good trap, so each check must hold
on all harts at once. Write tests in the style of piton/verif/diag/assembly/riscv/pico/lw.S, and read TEST_CASE in
piton/verif/diag/assembly/include/riscv/pico/test_macros.h:

  #include "riscv_test.h"
  #include "test_macros.h"
  RVTEST_RV32U
  RVTEST_CODE_BEGIN
    TEST_CASE( 2, x2, 0x1234, li x2, 0x1234; )
    TEST_PASSFAIL
  RVTEST_CODE_END
    .data
  RVTEST_DATA_BEGIN
    TEST_DATA
    (your data labels)
  RVTEST_DATA_END

TEST_CASE(number, register, expected value, instructions) runs the instructions and fails the test if the register
differs from the expected value. The numbers start at 2 and are unique. The preprocessor joins the instructions into
one line, so write no # comment inside TEST_CASE( ... ): it would hide the rest of the line, the check included.
Comment with /* */ inside the macro, or put a # comment on its own line outside it. End every test with
TEST_PASSFAIL, never RVTEST_PASS alone, and give each TEST_CASE a different number. Check only behavior the
instruction set defines; the core has no trap handler to test, so use no ecall, mtvec, or other CSR."""

_C_STYLE = """\
A directed test is a short C program that checks one piece of logic in the design. Every tile runs main on shared
memory, and the run passes only when main returns 0 on every hart, so a failed check must make main return nonzero.
Write tests in the style of the programs in piton/verif/diag/c/riscv/ariane/, and read
piton/verif/diag/assembly/include/riscv/ariane/util.h. argv[0][0] is the hart id and argv[0][1] is the number of harts:

  #include <stdint.h>
  #include <stdio.h>
  #include "util.h"
  int main(int argc, char **argv) {
    uint32_t hart_id = argv[0][0];
    int failed = 0;
    /* checks */
    return failed != 0;
  }"""


def test_form(core: str) -> str | None:
    """The form of test *core* takes (``"S"`` or ``"c"``), or ``None``."""
    return FORM_BY_CORE.get(core)


def test_tag(run_id: str | None, iteration: int, task_id: str) -> str:
    """A short name for one task's tests in one run, safe in a path and a tool name."""
    return hashlib.sha1(f"{run_id}:{iteration}:{task_id}".encode()).hexdigest()[:8]


def test_dir(piton_root: str, tag: str) -> str:
    """The directory on the checkout that holds the tests under *tag*."""
    return os.path.join(piton_root, TEST_DIR, tag)


def clear_tests(piton_root: str) -> None:
    """Remove every directed test a previous job left on a checkout."""
    shutil.rmtree(os.path.join(piton_root, TEST_DIR), ignore_errors=True)


def verdict_of(run: PitonRunResult) -> str:
    """*run*'s verdict, or ``no_verdict`` when the run reported none."""
    return run.verdict or "no_verdict"


def usable(tests) -> tuple[DirectedTest, ...]:
    """The tests among *tests* that ran to a verdict: the ones that can gate a task."""
    return tuple(t for t in tests if t.verdict not in UNUSABLE)


def merge_tests(task: Task, applied: dict[str, tuple[DirectedTest, ...]]) -> tuple[DirectedTest, ...]:
    """The tests of the tasks *task* depends on, in ``deps`` order. *applied*
    maps each passed task to every test its gates carried, its dependencies'
    included. A later test of the same name replaces an earlier one."""
    merged: dict[str, DirectedTest] = {}
    for dep in task.deps:
        for t in applied.get(dep, ()):
            merged[t.name] = t
    return tuple(merged.values())


def carry_tests(
    task: Task, own: tuple[DirectedTest, ...], applied: dict[str, tuple[DirectedTest, ...]]
) -> tuple[DirectedTest, ...]:
    """What a passed *task* hands to the tasks that depend on it: the tests
    it inherited, and its *own* that ran to a verdict."""
    merged = {t.name: t for t in merge_tests(task, applied)}
    merged.update({t.name: t for t in usable(own)})
    return tuple(merged.values())


def test_prompt(spec: MaceSpec, task: Task, form: str, context: str = "", feedback: str = "") -> str:
    """What a ``test`` task's agent is told. *context* is what earlier
    attempts in the run reported; *feedback* is why this task's previous
    attempt produced no usable test."""
    x, y = spec.target_mesh
    lines = [
        f"You are writing directed tests for OpenPiton/{spec.core} for one task of a MACE run.",
        "",
        f"Objective: {spec.objective}",
        f"Target mesh: {x}x{y} tiles ({x * y} harts)",
        f"Gate workloads (fixed programs written by people; they stay, and they must keep passing): "
        f"{', '.join(spec.workloads)}",
        "",
        f"Task {task.id}: {task.spec}",
    ]
    if context.strip():
        lines += ["", "What the earlier attempts in this run reported:", context.strip()]
    lines += ["", _ASM_STYLE if form == "S" else _C_STYLE, ""]
    lines.append(
        "Use the tools to read the design under piton/design/ and the example programs. Work out which logic the "
        "task is about, or which logic you suspect is wrong, and write up to "
        f"{MAX_TESTS_PER_TASK} tests with write_test that exercise it. Check each result against what the RISC-V "
        "specification or the documented behavior requires, not against what the RTL currently does. A test must "
        "pass on a correct design and fail when that logic is wrong, so give it inputs that tell a correct result "
        "from a wrong one. Keep each test short, a few hundred instructions at most. Set the target of each test "
        "to the RTL file the logic lives in. You cannot run anything yourself: after you finish, the design is "
        "built and each test is simulated on every tile. End with one line per test saying what it checks."
    )
    if feedback.strip():
        lines += ["", "Your previous attempt gave no usable test:", feedback.strip()]
    return "\n".join(lines)


def tests_report(tests: tuple[DirectedTest, ...]) -> str:
    """The directed tests a task's agent is shown: each one's source and
    how it ran on the design before the agent's change."""
    lines = []
    for t in tests:
        lines.append(f"- {t.program}, about {t.target}: {t.verdict} on the design before your change")
        lines.append("  Source:\n" + "\n".join("    " + s for s in t.source[:PROMPT_SOURCE_CHARS].splitlines()))
        if t.verdict != "pass" and t.evidence:
            lines.append("  End of its log:\n" + "\n".join("    " + s for s in t.evidence[-PROMPT_EVIDENCE_CHARS:].splitlines()))
    return "\n".join(lines)


def test_session(
    node, spec: MaceSpec, task: Task, llm, tools, form: str, directory: str, tag: str, context: str, feedback: str
) -> QueryResult:
    """One attempt of a ``test`` task's agent call, with a
    :class:`~mace.tools.DirectedTestTool` writing into *directory* when Ray
    is up. Returns the reply."""
    tool = None
    if ray.is_initialized():
        tool = DirectedTestTool(f"dt_{tag}", node.piton_root, directory, form, task_options=node.task_options)
    try:
        return usage.prompt_retrying(
            llm, "task", test_prompt(spec, task, form, context, feedback), (*tools, tool) if tool else tools
        )
    except Exception as e:
        if usage.is_rate_limit(e):
            raise  # not a failed attempt: once the waits are used the run ends as an error, which the batch runs again
        logger.warning("task %s: test agent call failed: %s", task.id, e)
        return QueryResult(result="", returncode=1, stderr=str(e), stream_result="")
    finally:
        if tool is not None:
            tool.stop()


def read_tests(node, directory: str) -> tuple[DirectedTest, ...]:
    """The tests an agent wrote into *directory* on the node's machine, from
    the sidecar its tool keeps; ``()`` when it wrote none."""
    try:
        collected = get(node.collect.chia_remote(directory, (TEST_SIDECAR,)))
        entries = json.loads(collected.files.get(TEST_SIDECAR) or "[]")
        return tuple(
            DirectedTest(name=e["name"], target=e["target"], form=e["form"], source=e["source"]) for e in entries
        )
    except Exception as e:  # noqa: BLE001 -- a missing or damaged sidecar means no tests
        logger.warning("no tests read from %s: %s", directory, e)
        return ()


def install_tests(node, tag: str, tests: tuple[DirectedTest, ...]) -> str:
    """Write *tests* into a directory of their own on the node's checkout
    (so a gate run finds them with ``asm_diag_root``); returns the directory."""
    directory = test_dir(node.piton_root, tag)
    rel = os.path.relpath(directory, node.piton_root)
    for t in tests:
        get(node.put_file.chia_remote(os.path.join(rel, t.program), t.source))
    return directory


_COMPILE_LOGS = ("rv32_as.log", "rv64_cc.log", "rv64_img.log")


def compile_log(node, run: PitonRunResult) -> str:
    """Why a test run reported no verdict: the assembler or compiler log in
    its run directory, else the end of sims' own output."""
    text = ""
    try:
        collected = get(node.collect.chia_remote(run.run_dir, _COMPILE_LOGS)) if run.run_dir else None
        if collected is not None:
            text = "\n".join(collected.files[name] for name in _COMPILE_LOGS if collected.files.get(name))
    except Exception as e:  # noqa: BLE001 -- the fallback below is enough
        logger.warning("no compile log from %s: %s", run.run_dir, e)
    return (text or "\n".join(s for s in (run.stdout, run.stderr, run.sim_log_tail) if s))[-EVIDENCE_CHARS:]


def run_test_attempts(
    spec: MaceSpec,
    task: Task,
    form: str,
    tag: str,
    root: str,
    *,
    session: Callable[[int, str, str], QueryResult],
    read: Callable[[str], tuple[DirectedTest, ...]],
    run_program: Callable[[str, str], PitonRunResult],
    explain: Callable[[PitonRunResult], str],
    out_of_time: Callable[[], bool] = lambda: False,
) -> tuple[tuple[DirectedTest, ...], tuple[PitonRunResult, ...], QueryResult | None]:
    """Ask the agent for tests and run them, until one runs to a verdict or
    :data:`MAX_TEST_ATTEMPTS` attempts are used.

    ``session(attempt, directory, feedback)`` is one agent call that writes
    into *directory* (under *root*, the checkout); ``read(directory)`` gives
    the tests it wrote; ``run_program(program, directory)`` simulates one;
    ``explain(run)`` says why a run gave no verdict. Returns every test
    written (each attempt's, with its verdict), every run made, and the last
    agent reply.
    """
    tests: list[DirectedTest] = []
    runs: list[PitonRunResult] = []
    query: QueryResult | None = None
    feedback = ""
    for attempt in range(1, MAX_TEST_ATTEMPTS + 1):
        if out_of_time():
            break
        directory = test_dir(root, f"{tag}a{attempt}")
        query = session(attempt, directory, feedback)
        written = read(directory)
        if not written:
            feedback = "You wrote no test. Call write_test with the name, the target RTL file, and the full source."
            continue
        problems = []
        ran = False
        for t in written:
            started = time.monotonic()
            run = run_program(t.program, directory)
            runs.append(run)
            verdict = verdict_of(run)
            if verdict == "no_verdict":
                evidence = explain(run)
                problems.append(f"{t.program} gave no verdict:\n{evidence}")
            else:
                ran = True
                evidence = "" if verdict == "pass" else run.sim_log_tail[-EVIDENCE_CHARS:]
            tests.append(
                dataclasses.replace(
                    t, attempt=attempt, verdict=verdict, evidence=evidence,
                    wall_s=run.wall_time_s or time.monotonic() - started,
                )
            )
        if ran:
            break
        feedback = "\n\n".join(problems)
    return tuple(tests), tuple(runs), query
