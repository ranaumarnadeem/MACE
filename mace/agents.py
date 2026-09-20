"""mace.agents -- footer parsers for LLM-produced plan/diagnosis text.

The Planner and the failure-analysis step communicate structured decisions
back to the loop through plain footer lines in their prose response, not a
JSON mode (no CHIA backend guarantees one -- see mace.test.conftest.FakeLLM's
docstring). These parsers are deliberately permissive about surrounding
text -- re.findall over the whole response, not a strict single-block
grammar -- because a real response is prose with directives embedded in it,
not a form.

TASK/DIAGNOSIS/FIX are matched case-insensitively, per line, anywhere in the
text. TASK lines are cumulative: a plan is many tasks, and every well-formed
one is kept. DIAGNOSIS/FIX are last-match-wins: a model that reconsiders
itself mid-response ("at first glance X ... actually, DIAGNOSIS: Y") should
have its final answer win, not its first draft.

A malformed line is dropped, not raised: the caller sees fewer tasks (or no
diagnosis) than the objective implied and treats that as "not enough to act
on" -- which is the actual fail-open behavior. Deciding what to do about a
short plan is the loop's job; this module only extracts what is there.
"""

from __future__ import annotations

import re

from chia_openpiton.state_def import DEFAULT_CACHES
from mace.spec import TASK_KINDS, Task

# Not enforced by parse_diagnosis -- see its docstring for why. Exposed so a
# caller can decide whether a parsed value is one it recognizes.
#
# testbench_mismatch is distinct from rtl_suspect: it means the scaffolded
# unit-test testbench's DUT port connections don't match the real module
# (see mace.unit_test_scaffold, mace.loop._run_unit_test_step) -- an edit to
# the testbench file fixes it, not the DUT. Conflating the two would send an
# agent to "fix" working RTL for what is actually a stale connection list.
KNOWN_DIAGNOSES: frozenset[str] = frozenset(
    ("test_bug", "config_error", "timeout", "maxcycles", "rtl_suspect", "testbench_mismatch")
)

# Same non-enforcement convention as KNOWN_DIAGNOSES -- see parse_assessment.
KNOWN_ASSESSMENTS: frozenset[str] = frozenset(
    ("fixable_config", "likely_hardware_limitation", "inconclusive")
)


def _footer_lines(text: str, tag: str) -> list[str]:
    """Every ``<tag>: ...`` line body in ``text``, in file order.

    The whitespace between the colon and the value is deliberately
    ``[ \\t]*``, not ``\\s*``: ``\\s`` matches ``\\n`` too, so with no value
    on the tag's own line ``\\s*`` would cross the line break and swallow
    the *entire next line* -- including that line's own tag -- as this
    line's value. A truly empty tag line is dropped instead (no match),
    matching this module's existing fail-open convention.
    """
    return re.findall(rf"(?im)^\s*{tag}:[ \t]*(.+)$", text)


def parse_tasks(text: str) -> tuple[Task, ...]:
    """Every well-formed ``TASK: <id> | deps=<ids> | kind=<kind> | <spec>`` line.

    Order matches the text. A line that doesn't split into exactly those four
    ``|``-separated fields, has a blank id, or a kind outside
    :data:`mace.spec.TASK_KINDS` is skipped rather than raising.

    A task whose id is also named in a well-formed ``CACHES:`` line (see
    :func:`parse_cache_overrides`) gets that line's cache geometry; every
    other task keeps ``caches=None`` (the mesh's default geometry).
    """
    cache_overrides = parse_cache_overrides(text)
    tasks = []
    for raw in _footer_lines(text, "TASK"):
        task = _parse_task_line(raw, cache_overrides)
        if task is not None:
            tasks.append(task)
    return tuple(tasks)


def _parse_task_line(
    raw: str, cache_overrides: dict[str, tuple[tuple[str, tuple[int, int]], ...]]
) -> Task | None:
    parts = raw.split("|", 3)
    if len(parts) != 4:
        return None
    task_id, deps_field, kind_field, spec = (p.strip() for p in parts)
    if not task_id or not deps_field.lower().startswith("deps=") or not kind_field.lower().startswith("kind="):
        return None
    kind = kind_field[len("kind="):].strip().lower()
    if kind not in TASK_KINDS:
        return None
    deps = tuple(d.strip() for d in deps_field[len("deps="):].split(",") if d.strip())
    try:
        return Task(id=task_id, deps=deps, kind=kind, spec=spec, caches=cache_overrides.get(task_id))
    except ValueError:
        return None


def parse_cache_overrides(text: str) -> dict[str, tuple[tuple[str, tuple[int, int]], ...]]:
    """Every well-formed ``CACHES: <task_id> | <name>=<size>,<assoc> ...``
    line, keyed by task id, as a sorted tuple of ``(name, (size, assoc))``
    pairs ready for :attr:`mace.spec.Task.caches`.

    Only ``l1i``, ``l1d``, ``l15``, ``l2`` (:data:`chia_openpiton.state_def.
    DEFAULT_CACHES`) are recognized names. Multiple ``CACHES:`` lines for the
    same task id merge, a later line's value for a given cache name winning.
    A malformed line, or a malformed individual ``<name>=<size>,<assoc>``
    entry within an otherwise well-formed line, is dropped rather than
    raised -- same fail-open convention as :func:`parse_tasks`.
    """
    merged: dict[str, dict[str, tuple[int, int]]] = {}
    for raw in _footer_lines(text, "CACHES"):
        parsed = _parse_cache_line(raw)
        if parsed is None:
            continue
        task_id, caches = parsed
        merged.setdefault(task_id, {}).update(caches)
    return {task_id: tuple(sorted(caches.items())) for task_id, caches in merged.items()}


def _parse_cache_line(raw: str) -> tuple[str, dict[str, tuple[int, int]]] | None:
    task_id, sep, rest = raw.partition("|")
    if not sep:
        return None
    task_id = task_id.strip()
    if not task_id:
        return None
    caches: dict[str, tuple[int, int]] = {}
    for token in rest.split():
        name, eq, geom = token.partition("=")
        name = name.strip().lower()
        if not eq or name not in DEFAULT_CACHES:
            continue
        size_str, comma, assoc_str = geom.partition(",")
        if not comma:
            continue
        try:
            size, assoc = int(size_str), int(assoc_str)
        except ValueError:
            continue
        if size <= 0 or assoc <= 0:
            continue
        caches[name] = (size, assoc)
    return (task_id, caches) if caches else None


def parse_diagnosis(text: str) -> str | None:
    """The last ``DIAGNOSIS: ...`` value, lowercased; ``None`` if absent.

    Not validated against :data:`KNOWN_DIAGNOSES` here -- the taxonomy is a
    caller-level policy question (is this a diagnosis we act on?), not an
    extraction question (what did the text say?), and hard-coding it into
    the parser would mean the parser has to change every time the taxonomy
    does.
    """
    lines = _footer_lines(text, "DIAGNOSIS")
    return lines[-1].strip().lower() if lines else None


def parse_fix(text: str) -> str | None:
    """The last ``FIX: ...`` value, verbatim; ``None`` if absent."""
    lines = _footer_lines(text, "FIX")
    return lines[-1].strip() if lines else None


def parse_assessment(text: str) -> str | None:
    """The last ``ASSESSMENT: ...`` value, lowercased; ``None`` if absent.

    Not validated against :data:`KNOWN_ASSESSMENTS` here, for the same reason
    :func:`parse_diagnosis` doesn't validate against :data:`KNOWN_DIAGNOSES`
    -- see that docstring.
    """
    lines = _footer_lines(text, "ASSESSMENT")
    return lines[-1].strip().lower() if lines else None


def parse_explanation(text: str) -> str | None:
    """The last ``EXPLANATION: ...`` value, verbatim; ``None`` if absent."""
    lines = _footer_lines(text, "EXPLANATION")
    return lines[-1].strip() if lines else None


def parse_next_steps(text: str) -> str | None:
    """The last ``NEXT_STEPS: ...`` value, verbatim; ``None`` if absent."""
    lines = _footer_lines(text, "NEXT_STEPS")
    return lines[-1].strip() if lines else None


# The real Verilator error for a testbench instantiation naming a port that
# doesn't exist on the DUT -- captured by deliberately renaming a working
# port connection in piton/verif/env/pico_reset_ut/pico_reset_ut_top.v and
# rebuilding, not guessed:
#   %Error-PINNOTFOUND: <file>:<line>: Pin not found: 'mem_valid_WRONG'
# %Warning-PINMISSING (an unconnected DUT pin) is a separate, non-fatal
# signature and is deliberately not matched here -- it doesn't fail the
# build, and an unconnected pin is often intentional (see
# mace.loop._run_unit_test_step's own tie-offs), unlike a nonexistent one.
_PINNOTFOUND = re.compile(r"%Error-PINNOTFOUND\b")


def is_testbench_port_mismatch(build_stderr: str) -> bool:
    """Whether a build's stderr shows the real signature of a scaffolded
    unit-test testbench naming a DUT port that doesn't exist -- see
    :data:`KNOWN_DIAGNOSES`'s ``testbench_mismatch`` entry for why this is
    kept distinct from an ``rtl_suspect`` diagnosis.
    """
    return bool(_PINNOTFOUND.search(build_stderr))
