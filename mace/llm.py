"""mace.llm -- pick an LLM backend by env var, one call site for all of them.

``MACE_LLM`` selects which real chia.models backend the loop talks to:
``vertex`` (default, the same default as every CLI), ``opencode``,
``claude``, or ``antigravity``. Model and other per-backend knobs are
separate (``MACE_LLM_MODEL`` env var, or keyword overrides), so switching
backends is a config change, not a call-site change -- mace.loop and
mace.planner only ever see an LLMCallBase, never a specific class.

Each backend module is imported lazily, inside its own builder function:
their SDKs (anthropic, google-genai, ...) are optional dependencies this
project does not otherwise need, matching chia.models.vertex's own lazy
import of google-genai.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from chia.base.llm_call import LLMCallBase, QueryResult


class UnknownLLMBackendError(ValueError):
    """MACE_LLM (or the backend argument) named something not recognized."""


# Vertex list prices in US dollars per million tokens, as (input, output).
# Gemini bills thinking tokens at the output price. These are the Gemini 2.5
# Flash prices the hackathon paper used; a model missing here costs 0.0 while
# its tokens are still counted.
VERTEX_USD_PER_M_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
}


def vertex_cost_usd(model: str, input_tokens: int, output_tokens: int, thinking_tokens: int) -> float:
    """Dollar cost of one Vertex call at :data:`VERTEX_USD_PER_M_TOKENS`."""
    prices = VERTEX_USD_PER_M_TOKENS.get(model)
    if prices is None:
        return 0.0
    input_price, output_price = prices
    return (input_tokens * input_price + (output_tokens + thinking_tokens) * output_price) / 1e6


@dataclass
class VertexQueryResult(QueryResult):
    """:class:`QueryResult` that carries one Vertex call's token counts.

    ``usage`` holds ``model``, ``input_tokens``, ``output_tokens``,
    ``thinking_tokens``, and ``cost_usd``, summed over every model turn of
    the call. It rides on the reply, so it survives a ``.chia_remote(...)``
    round-trip, as ``OpenCodeQueryResult.usage`` does.
    """

    usage: Optional[dict] = None


# This project's only funded backend -- confirmed reachable on its GCP
# project as of 2026-09-19. Only vertex gets a hardcoded default: forcing
# it onto opencode/claude/antigravity fed a vertex-only model id straight
# into their constructors regardless of --backend (a real regression --
# see default_model_for_backend's docstring).
DEFAULT_VERTEX_MODEL = "gemini-2.5-flash"


def default_model_for_backend(model: str | None, backend: str) -> str | None:
    """*model* if given; otherwise :data:`DEFAULT_VERTEX_MODEL` for
    ``backend == "vertex"``, or ``None`` for any other backend so it falls
    back to its own default instead.

    Every call site that wants a zero-flag default for vertex used to
    default its own ``--model``/``model`` value to ``DEFAULT_VERTEX_MODEL``
    unconditionally, which then got forced onto every backend regardless of
    which one was actually selected. Centralized here so the
    backend-conditional part of the logic exists in exactly one place.
    """
    if model is not None:
        return model
    return DEFAULT_VERTEX_MODEL if backend == "vertex" else None


def extract_cost_usd(query: QueryResult) -> float:
    """Best-effort $ cost of one LLM call, from whatever its backend reports.

    Only backends whose QueryResult subclass carries usage data on the
    result itself survive a ``.chia_remote(...)`` round-trip:
    ``OpenCodeQueryResult.usage``, ``AntigravityQueryResult.usage``, and
    :class:`VertexQueryResult` ``.usage`` (from :mod:`mace.vertex`) do.
    Claude's cost tracking lives on the ``LLMCallBase`` instance's own
    ``_last_metadata`` instead (chia.models.claude), which is a different
    copy on the remote worker after dispatch and never visible back here,
    so this returns ``0.0`` for that backend.
    """
    usage = getattr(query, "usage", None)
    if not usage:
        return 0.0
    return float(usage.get("cost_usd", 0.0) or 0.0)


def _build_opencode(model, overrides):
    from chia.models.opencode import OpenCodeLLM

    kwargs = {"model": model} if model else {}
    kwargs.update(overrides)
    return OpenCodeLLM(**kwargs)


def _build_claude(model, overrides):
    from chia.models.claude import ClaudeCodeLLM

    kwargs = {"model": model} if model else {}
    kwargs.update(overrides)
    return ClaudeCodeLLM(**kwargs)


def _build_antigravity(model, overrides):
    from chia.models.antigravity import AntigravityLLM

    kwargs = {"model": model} if model else {}
    kwargs.update(overrides)
    return AntigravityLLM(**kwargs)


def _build_vertex(model, overrides):
    from mace.vertex import UsageVertexLLM

    kwargs = {"model": model} if model else {}
    kwargs.update(overrides)
    return UsageVertexLLM(**kwargs)  # raises TypeError if no model ends up set


_BACKENDS = {
    "opencode": _build_opencode,
    "claude": _build_claude,
    "antigravity": _build_antigravity,
    "vertex": _build_vertex,
}


def make_llm(backend: str | None = None, **overrides) -> LLMCallBase:
    """Construct the backend named by *backend*, or the ``MACE_LLM`` env var.

    With neither set, the backend is ``vertex``. ``MACE_LLM_MODEL`` (if set)
    becomes the backend's ``model``, and vertex falls back to
    :data:`DEFAULT_VERTEX_MODEL`; *overrides* are forwarded to the backend's
    constructor verbatim and take precedence over both, so
    ``make_llm(model="...")`` always wins.

    Raises:
        UnknownLLMBackendError: the backend name isn't one of opencode/
            claude/antigravity/vertex.
    """
    backend = (backend or os.environ.get("MACE_LLM", "vertex")).lower()
    builder = _BACKENDS.get(backend)
    if builder is None:
        raise UnknownLLMBackendError(
            f"MACE_LLM must be one of {sorted(_BACKENDS)}, got {backend!r}"
        )
    model = default_model_for_backend(os.environ.get("MACE_LLM_MODEL"), backend)
    return builder(model, overrides)
