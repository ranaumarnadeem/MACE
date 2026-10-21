"""mace.plan_check -- reject a plan the loop cannot carry out, before any build.

A plan costs one LLM call, and running it costs builds and simulations. Some
plans cannot work whatever the design does, and the run records show the
planner writing them again after a failure. :func:`check_plan` finds those
plans from the plan and the run's inputs alone, so the orchestrator can send
the reasons back to the planner for the price of one more plan call.

Each rule comes from a recorded failure:

- ``source_edit``: a task that needs a source edit in a run without RTL
  edits. The planner turned triage's advice to edit ``manycore_top.v`` into
  config tasks, which change only what ``CACHES:`` and ``CONFIG_RTL:`` set.
- ``required_define``: a task that builds without an RTL define the objective
  says it needs. The task that carried the define depended on the one
  without it, so the first level failed and the second never ran.
- ``way_rule``: an Ariane cache geometry the build refuses, which costs an
  iteration and a triage call.
- ``missing_module``: a ``unit_test`` of an RTL path that does not exist.
- ``repeated_failure``: a task that builds a configuration which already
  failed in this run. Builds are deterministic, so it fails again.
- ``known_bad_change``: a task that makes a change :mod:`mace.narrowing` showed
  breaks the design, such as the seeded define a re-plan added back beside
  another change, so that it was no longer a repeat of the failed build.
- ``wrapper_skeleton``: while a generic-tile core's wrapper is still the
  skeleton, a config or workload task that does not depend on an ``rtl``
  task, and a plan with no ``rtl`` task. No core runs on such a build. The
  planner spent all five iterations of two Ibex runs on config tasks, and
  in others put a config task first, so the ``rtl`` task after it never ran.

The check never rejects a plan for being unwise, only for being unable to
work, and the orchestrator stops asking after a few rejections so the
simulator stays the final judge.

One more thing is fixed without asking the planner: :func:`strip_unknown_defines`
removes a define no file of the checkout mentions. The planner invents names
such as ``CONFIG_HANDLE_ASYNC_MUX_FOR_FPGA``, and a define nothing tests
changes nothing, so a plan that carries one is not wrong, only wasteful: it
builds a second model that behaves like the first. Replayed over 1005
recorded plans, rejecting such a plan would have sent back 103 that passed.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from mace.integrator import NOT_STARTED, topological_order
from mace.loop import _config_for_task
from mace.narrowing import Change, config_changes, reference_config
from mace.spec import MaceSpec, StepResult, Task

SOURCE_EDIT = "source_edit"
REQUIRED_DEFINE = "required_define"
WAY_RULE = "way_rule"
MISSING_MODULE = "missing_module"
REPEATED_FAILURE = "repeated_failure"
KNOWN_BAD_CHANGE = "known_bad_change"
WRAPPER_SKELETON = "wrapper_skeleton"

# The tasks that build the whole chip and simulate the gate workloads.
_BUILD_KINDS = ("config", "workload")


@dataclass(frozen=True)
class PlanProblem:
    """One reason a plan cannot work: the *rule* that found it, the task it
    is about, and *reason*, worded for the planner."""

    rule: str
    task_id: str
    reason: str


# -------------------------------------------------------------- objective defines

_DEFINE_NAME = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
_MENTIONS_DEFINE = re.compile(r"\bdefines?\b", re.IGNORECASE)


def objective_defines(objective: str) -> tuple[str, ...]:
    """The RTL defines *objective* says a build needs: the upper-snake-case
    names in the sentences that mention a define, in order. A name in a
    sentence that does not (a run argument, an environment variable) is not
    a define the build must set."""
    found: list[str] = []
    for sentence in _SENTENCE_BREAK.split(objective):
        if _MENTIONS_DEFINE.search(sentence):
            found.extend(n for n in _DEFINE_NAME.findall(sentence) if n not in found)
    return tuple(found)


# -------------------------------------------------------------- source edits

_HDL_FILE = re.compile(r"[A-Za-z_][\w./-]*\.(?:v|sv|vh|svh|pyv)\b")
_EDIT_VERB = re.compile(
    r"\b(?:edit|modify|change|update|fix|patch|replace|rewrite|correct|adjust|comment out|remove|delete|"
    r"insert|declare|instantiate|connect|disconnect)\w*",
    re.IGNORECASE,
)


def names_source_edit(instruction: str) -> str | None:
    """The HDL file *instruction* asks to edit, or ``None``: an edit verb
    and a Verilog or SystemVerilog file name in the same instruction."""
    match = _HDL_FILE.search(instruction)
    if match is None or _EDIT_VERB.search(instruction) is None:
        return None
    return match.group(0)


# -------------------------------------------------------------- the define index

# Where OpenPiton's defines are used: the RTL and its generators, the testbench
# and monitors, and the tools that turn -config_rtl flags into defines and
# build the Verilator model (sims, and the C++ harness).
INDEX_DIRS = ("piton/design", "piton/verif", "piton/tools/src", "piton/tools/verilator")
_SKIP_DIRS = frozenset((".git", "build", "obj_dir"))
_SKIP_SUFFIXES = frozenset((
    ".o", ".a", ".so", ".bin", ".elf", ".dat", ".hex", ".mem", ".vcd", ".gz", ".tar", ".zip", ".png", ".pdf",
    ".jpg", ".rpt", ".log", ".db", ".exe", ".img", ".dtb", ".bit",
))
_MAX_FILE_BYTES = 4_000_000
_DEFINE_TOKEN = re.compile(rb"\b[A-Z][A-Z0-9_]+\b")


@functools.lru_cache(maxsize=4)
def define_index(checkout: str) -> frozenset[str]:
    """Every upper-snake-case word in the text files under *checkout*'s
    design, verification, and tool sources.

    A define a file spells out anywhere, not only after ``ifdef``, counts as
    used: sims and the testbench also read defines through scripts and flag
    lists, and a missed use would reject a plan that works. Computed once per
    checkout and process.
    """
    words: set[bytes] = set()
    for sub in INDEX_DIRS:
        for directory, subdirs, files in os.walk(os.path.join(checkout, sub)):
            subdirs[:] = [d for d in subdirs if d not in _SKIP_DIRS]
            for name in files:
                if os.path.splitext(name)[1].lower() in _SKIP_SUFFIXES:
                    continue
                path = os.path.join(directory, name)
                try:
                    if os.path.islink(path) or os.path.getsize(path) > _MAX_FILE_BYTES:
                        continue
                    with open(path, "rb") as f:
                        words.update(_DEFINE_TOKEN.findall(f.read()))
                except OSError:
                    continue
    return frozenset(w.decode("ascii", "replace") for w in words)


REQUIRED_DEFINE_ADDED = "required_define_added"
# Every task kind that builds the chip: config and workload tasks, and rtl tasks, which build the edited design.
_DEFINE_KINDS = ("config", "workload", "rtl")


def add_required_defines(
    tasks: tuple[Task, ...], required: tuple[str, ...]
) -> tuple[tuple[Task, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    """*tasks* with every define in *required* added to each task that builds,
    and, for each task changed, its id and the defines added.

    The objective names these defines as ones the build needs, so fixed code
    adds them rather than asking the planner again: a planner that leaves one
    out of an rtl task builds a design that cannot pass."""
    out = []
    added: list[tuple[str, tuple[str, ...]]] = []
    for task in tasks:
        missing = tuple(d for d in required if d not in (task.config_rtl or ()))
        if task.kind not in _DEFINE_KINDS or not missing:
            out.append(task)
            continue
        out.append(dataclasses.replace(task, config_rtl=tuple(sorted({*(task.config_rtl or ()), *missing}))))
        added.append((task.id, missing))
    return tuple(out), tuple(added)


def strip_unknown_defines(
    tasks: tuple[Task, ...], known_defines: frozenset[str] | None, keep: tuple[str, ...] = ()
) -> tuple[tuple[Task, ...], tuple[str, ...]]:
    """*tasks* without the defines in no file of the checkout, and the sorted
    names removed. *known_defines* is :func:`define_index` (``None`` strips
    nothing); a define in *keep*, such as one the objective names, stays.
    A task left with no define gets ``config_rtl=None``."""
    if known_defines is None:
        return tasks, ()
    removed: set[str] = set()
    out = []
    for task in tasks:
        flags = task.config_rtl or ()
        unknown = [f for f in flags if f not in known_defines and f not in keep]
        if not unknown:
            out.append(task)
            continue
        removed.update(unknown)
        kept = tuple(f for f in flags if f not in unknown)
        out.append(dataclasses.replace(task, config_rtl=kept or None))
    return tuple(out), tuple(sorted(removed))


# -------------------------------------------------------------- the check


def config_key(spec: MaceSpec, task: Task) -> tuple[str, ...]:
    """The build flags *task* gets under *spec*: two tasks with equal keys
    build the same model."""
    return tuple(_config_for_task(spec, task).sims_flags())


def note_failed_config(failed: dict[tuple[str, ...], str], result: StepResult, iteration: int) -> None:
    """Add *result*'s configuration to *failed* when its task failed on its
    own merits: not for a pass, a build that never started because the run's
    time was gone, or a build that carried RTL edits, which the flags do not
    show. The first failure of a configuration is the one kept."""
    if result.passed or result.edits or result.build.failure_reason == NOT_STARTED:
        return
    if not result.build.success:
        how = "its build failed"
    else:
        verdict = result.run.verdict if result.run is not None else None
        how = f"its simulation ended {verdict}"
    failed.setdefault(
        tuple(result.build.config.sims_flags()),
        f"task {result.task.id} in iteration {iteration + 1} ({how})",
    )


def _builds_on_edits(tasks: tuple[Task, ...]) -> set[str]:
    """Ids of the tasks that are, or depend on, an ``rtl`` task: their build
    carries RTL edits their configuration does not show."""
    edited: set[str] = set()
    for task in topological_order(tasks):
        if task.kind == "rtl" or any(dep in edited for dep in task.deps):
            edited.add(task.id)
    return edited


def check_plan(
    spec: MaceSpec,
    tasks: tuple[Task, ...],
    *,
    rtl_edits: bool = False,
    path_exists: Callable[[str], bool] | None = None,
    failed_configs: Mapping[tuple[str, ...], str] | None = None,
    bad_changes: Sequence[tuple[Change, ...]] = (),
    unwritten_wrapper: str | None = None,
) -> tuple[PlanProblem, ...]:
    """Why *tasks* cannot work as a plan for *spec*; empty when they may.

    *rtl_edits* says whether the run allows ``rtl`` tasks. *unwritten_wrapper*
    is the path of a generic-tile core's wrapper that still holds its
    skeleton (see :func:`mace.generic_core.unwritten_wrapper`); a plan with no
    ``rtl`` task, and each config or workload task that does not depend on
    one, are then rejected. *path_exists*
    enables the ``missing_module`` rule; a caller with no checkout leaves it
    out. *failed_configs* maps the build flags of each configuration that
    already failed in the run (see :func:`config_key`) to a short account of
    how, and enables ``repeated_failure``. *bad_changes* are the sets of
    changes narrowing showed break the design; a task that makes every
    change of one set is rejected. Run :func:`strip_unknown_defines` on
    *tasks* first, so the flags compared are the flags that would build.
    """
    problems: list[PlanProblem] = []
    if unwritten_wrapper and rtl_edits and tasks:
        if not any(t.kind == "rtl" for t in tasks):
            problems.append(PlanProblem(
                WRAPPER_SKELETON, "",
                f"the core's wrapper, {unwritten_wrapper}, still holds its 'wrapper body' skeleton, so no core runs "
                "on any build and every config or workload task times out. Add an rtl task that writes the wrapper, "
                "and make the other tasks depend on it.",
            ))
        after_rtl = _builds_on_edits(tasks)
        for task in tasks:
            if task.kind in _BUILD_KINDS and task.id not in after_rtl:
                problems.append(PlanProblem(
                    WRAPPER_SKELETON, task.id,
                    f"task {task.id} builds before any rtl task has written the core's wrapper, {unwritten_wrapper}, "
                    "which still holds its 'wrapper body' skeleton, so no core runs and the task can only time out. "
                    "A config task cannot change which files are built. Make the task depend on an rtl task that "
                    "writes the wrapper.",
                ))
    required = objective_defines(spec.objective)
    edited = _builds_on_edits(tasks) if failed_configs or bad_changes else set()
    reference = reference_config(spec, required) if bad_changes else None

    for task in tasks:
        if task.kind == "rtl" and not rtl_edits:
            problems.append(PlanProblem(
                SOURCE_EDIT, task.id,
                f"task {task.id} has kind rtl, but no task in this run can edit a source file; use config or workload.",
            ))
        if task.kind in _BUILD_KINDS and not rtl_edits:
            edited_file = names_source_edit(task.spec)
            if edited_file is not None:
                problems.append(PlanProblem(
                    SOURCE_EDIT, task.id,
                    f"task {task.id} asks for an edit to {edited_file}, but no task in this run can edit a source "
                    "file: a config or workload task changes only what its CACHES: and CONFIG_RTL: lines set.",
                ))

        if task.kind == "unit_test" and path_exists is not None:
            path = task.spec.strip().strip("`'\"")
            if not path_exists(path):
                problems.append(PlanProblem(
                    MISSING_MODULE, task.id,
                    f"task {task.id} is a unit_test of {path}, which does not exist in the checkout; name an RTL "
                    "file that does, as a path relative to the checkout root.",
                ))

        if task.kind not in _BUILD_KINDS:
            continue

        missing = [d for d in required if d not in (task.config_rtl or ())]
        if missing:
            problems.append(PlanProblem(
                REQUIRED_DEFINE, task.id,
                f"task {task.id} builds without {' '.join(missing)}, which the objective says the build needs; "
                f"add a CONFIG_RTL: {task.id} | {' '.join(missing)} line (a task also builds with the defines of "
                "the tasks it depends on).",
            ))

        violations = _config_for_task(spec, task).way_violations()
        if violations:
            problems.append(PlanProblem(
                WAY_RULE, task.id,
                f"task {task.id}'s caches break Ariane's way rule and the build refuses them: "
                + "; ".join(violations) + ".",
            ))

        if bad_changes and task.id not in edited:
            made = set(config_changes(_config_for_task(spec, task), reference))
            for bad in bad_changes:
                if set(bad) <= made:
                    problems.append(PlanProblem(
                        KNOWN_BAD_CHANGE, task.id,
                        f"task {task.id} {' and '.join(c.describe() for c in bad)}, which a probe earlier in this "
                        f"run showed breaks the design: {'; '.join(c.remedy() for c in bad)}.",
                    ))
                    break

        if failed_configs and task.id not in edited:
            earlier = failed_configs.get(config_key(spec, task))
            if earlier is not None:
                problems.append(PlanProblem(
                    REPEATED_FAILURE, task.id,
                    f"task {task.id} builds the same configuration as {earlier}; builds are deterministic, so it "
                    "fails the same way. Change the configuration.",
                ))
    return tuple(problems)


UNUSABLE_REPLY = "unusable_reply"


def format_unusable_reply(error: str, rtl_edits: bool = False) -> str:
    """The note a re-plan call gets when the planner's last reply held no
    usable plan, such as a ``TASK:`` line without its ``kind=`` field or an
    empty reply: what was wrong, and the line format again. *error* is the
    :class:`~mace.planner.PlanningError` message."""
    kinds = "config|workload|unit_test|rtl" if rtl_edits else "config|workload|unit_test"
    return (
        f"Your last reply could not be read as a plan: {error[:400]}\n"
        "Reply with one line per task, in exactly this format:\n"
        f"TASK: <id> | deps=<comma-separated task ids, or empty> | kind=<{kinds}> | <short instruction>\n"
        "Every task line needs all four fields, and the third starts with kind=."
    )


def format_rejection(problems: tuple[PlanProblem, ...]) -> str:
    """*problems* as the note a re-plan call gets: what was wrong, and that
    no build or simulation was spent on it."""
    lines = [
        "The plan you just gave was rejected before any build, because it cannot work:",
        *(f"- {p.reason}" for p in problems),
        "Plan again without these problems, and keep everything else the objective asks for.",
    ]
    return "\n".join(lines)
