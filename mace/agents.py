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

from mace.spec import TASK_KINDS, Task

# Not enforced by parse_diagnosis -- see its docstring for why. Exposed so a
# caller can decide whether a parsed value is one it recognizes.
KNOWN_DIAGNOSES: frozenset[str] = frozenset(
    ("test_bug", "config_error", "timeout", "maxcycles", "rtl_suspect")
)


def _footer_lines(text: str, tag: str) -> list[str]:
    """Every ``<tag>: ...`` line body in ``text``, in file order."""
    return re.findall(rf"(?im)^\s*{tag}:\s*(.+)$", text)


def parse_tasks(text: str) -> tuple[Task, ...]:
    """Every well-formed ``TASK: <id> | deps=<ids> | kind=<kind> | <spec>`` line.

    Order matches the text. A line that doesn't split into exactly those four
    ``|``-separated fields, has a blank id, or a kind outside
    :data:`mace.spec.TASK_KINDS` is skipped rather than raising.
    """
    tasks = []
    for raw in _footer_lines(text, "TASK"):
        task = _parse_task_line(raw)
        if task is not None:
            tasks.append(task)
    return tuple(tasks)


def _parse_task_line(raw: str) -> Task | None:
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
        return Task(id=task_id, deps=deps, kind=kind, spec=spec)
    except ValueError:
        return None


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
