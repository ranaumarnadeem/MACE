"""mace.llm -- pick an LLM backend by env var, one call site for all of them.

``MACE_LLM`` selects which real chia.models backend the loop talks to:
``opencode`` (default, used all session so far), ``claude``,
``antigravity``, or ``vertex``. Model and other per-backend knobs are
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

from chia.base.llm_call import LLMCallBase, QueryResult


class UnknownLLMBackendError(ValueError):
    """MACE_LLM (or the backend argument) named something not recognized."""


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
    ``OpenCodeQueryResult.usage`` and ``AntigravityQueryResult.usage`` both
    do. Claude's cost tracking lives on the ``LLMCallBase`` instance's own
    ``_last_metadata`` instead (chia.models.claude), which is a different
    copy on the remote worker after dispatch and never visible back here --
    so this always returns ``0.0`` for that backend. **Vertex, this
    project's only funded backend, is the same story**: ``VertexGeminiLLM``
    returns the plain base ``QueryResult`` (chia.base.llm_call), which has
    no ``usage`` field at all -- confirmed against chia's own source, not
    guessed -- so every ``compute_usd`` figure recorded from a real vertex
    run (both captured baseline logs show ``compute_usd: 0.0``) is an
    unconditional zero, not a partial real measurement of a genuinely free
    call. Not a bug to fix in this function: a real API-shape limitation
    this project doesn't control, not something worth papering over with a
    wrong number.
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
    from chia.models.vertex import VertexGeminiLLM

    kwargs = {"model": model} if model else {}
    kwargs.update(overrides)
    return VertexGeminiLLM(**kwargs)  # raises TypeError if no model ends up set


_BACKENDS = {
    "opencode": _build_opencode,
    "claude": _build_claude,
    "antigravity": _build_antigravity,
    "vertex": _build_vertex,
}


def make_llm(backend: str | None = None, **overrides) -> LLMCallBase:
    """Construct the backend named by *backend*, or the ``MACE_LLM`` env var.

    ``MACE_LLM_MODEL`` (if set) becomes the backend's ``model``; *overrides*
    are forwarded to the backend's constructor verbatim and take precedence
    over it, so ``make_llm(model="...")`` always wins.

    Raises:
        UnknownLLMBackendError: the backend name isn't one of opencode/
            claude/antigravity/vertex.
    """
    backend = (backend or os.environ.get("MACE_LLM", "opencode")).lower()
    builder = _BACKENDS.get(backend)
    if builder is None:
        raise UnknownLLMBackendError(
            f"MACE_LLM must be one of {sorted(_BACKENDS)}, got {backend!r}"
        )
    model = os.environ.get("MACE_LLM_MODEL")
    return builder(model, overrides)
