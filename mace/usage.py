"""mace.usage -- one record per LLM call: phase, tokens, dollars, and time.

Every LLM call a run makes goes through :func:`prompt` (a local call) or
:func:`note` (after a ``.chia_remote(...)`` call resolves), tagged with the
phase that made it: ``plan``, ``task``, ``triage``, ``post_mortem``, or a
baseline's own phase. While a :class:`UsageLog` is active through
:func:`recording`, each call lands in it; with none active, nothing is
recorded, so code that never starts a log (tests, one-off scripts) is
unaffected.

The active log is process-wide, so one run records at a time per process,
which holds for every driver here: the end-to-end script, the batch runner,
and the CLI shell each run one loop at a time. Task calls come back on the
integrator's worker threads, so :meth:`UsageLog.add` takes a lock.

Token counts come from the reply's ``usage`` dict (see
:class:`mace.llm.VertexQueryResult`). A reply without one, such as a
``FakeLLM`` reply, records zero tokens and zero dollars. A call that raises
records its wall time with ``ok=False`` and zero tokens.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMCall:
    """One LLM call's cost and reply."""

    phase: str
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    usd: float = 0.0
    wall_s: float = 0.0
    ok: bool = True
    # The reply's text, so a run's plans and diagnoses can be read later.
    reply: str = ""


def call_from(phase: str, query, wall_s: float) -> LLMCall:
    """An :class:`LLMCall` for *query*, the reply a call in *phase* returned."""
    usage = getattr(query, "usage", None) or {}
    return LLMCall(
        phase=phase,
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        thinking_tokens=int(usage.get("thinking_tokens", 0) or 0),
        usd=float(usage.get("cost_usd", 0.0) or 0.0),
        wall_s=wall_s,
        ok=bool(getattr(query, "success", False)),
        reply=str(getattr(query, "result", "") or ""),
    )


class UsageLog:
    """The LLM calls one run made, in the order they finished."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[LLMCall] = []
        self._taken = 0

    def add(self, call: LLMCall) -> None:
        with self._lock:
            self._calls.append(call)

    def calls(self) -> tuple[LLMCall, ...]:
        with self._lock:
            return tuple(self._calls)

    def take(self) -> tuple[LLMCall, ...]:
        """The calls added since the previous :meth:`take`."""
        with self._lock:
            new = tuple(self._calls[self._taken :])
            self._taken = len(self._calls)
            return new

    def total_usd(self) -> float:
        with self._lock:
            return sum(c.usd for c in self._calls)


_active: UsageLog | None = None


@contextmanager
def recording(log: UsageLog):
    """Make *log* the active log for the duration of the block."""
    global _active
    previous, _active = _active, log
    try:
        yield log
    finally:
        _active = previous


def note(phase: str, query, wall_s: float) -> None:
    """Record a finished call in the active log, if there is one."""
    if _active is not None:
        _active.add(call_from(phase, query, wall_s))


def note_failure(phase: str, wall_s: float) -> None:
    """Record a call that raised, with no tokens known."""
    if _active is not None:
        _active.add(LLMCall(phase=phase, wall_s=wall_s, ok=False))


def prompt(llm, phase: str, message: str, tools=()):
    """``llm.prompt(message, tools=...)``, recorded under *phase*."""
    started = time.monotonic()
    try:
        query = llm.prompt(message, tools=list(tools))
    except Exception:
        note_failure(phase, time.monotonic() - started)
        raise
    note(phase, query, time.monotonic() - started)
    return query
