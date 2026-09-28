"""mace.vertex -- CHIA's Vertex backend, with each reply carrying its token counts.

CHIA's ``VertexGeminiLLM`` sums Gemini's input and output tokens into
``_last_metadata``, which stays in whichever process ran the call, and it
leaves out thinking tokens, which Gemini bills at the output price. For a
``.chia_remote(...)`` call only the reply comes back to the driver, so
:class:`UsageVertexLLM` puts every count on the reply itself, as a
:class:`~mace.llm.VertexQueryResult`.

The counts come from each ``generate_content`` response's
``usage_metadata``. CHIA creates its ``genai.Client`` inside the call, so
the class method is wrapped for the duration of one call, under a lock that
keeps two calls in one process from wrapping it at once. A call that raises
records nothing: a reply cut off at the output limit, or every retry
failing, spends tokens that these counts miss.
"""

from __future__ import annotations

import threading

from chia.models.vertex import VertexGeminiLLM

from mace.llm import VertexQueryResult, vertex_cost_usd

_WRAP_LOCK = threading.Lock()


def usage_from_responses(model: str, responses: list) -> dict:
    """``usage`` for :class:`~mace.llm.VertexQueryResult`, summed over
    *responses* (one per model turn of a call)."""
    counts = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    fields = {
        "input_tokens": "prompt_token_count",
        "output_tokens": "candidates_token_count",
        "thinking_tokens": "thoughts_token_count",
    }
    for response in responses:
        metadata = getattr(response, "usage_metadata", None)
        if metadata is None:
            continue
        for key, field in fields.items():
            counts[key] += getattr(metadata, field, 0) or 0
    return {
        "model": model,
        **counts,
        "cost_usd": vertex_cost_usd(
            model, counts["input_tokens"], counts["output_tokens"], counts["thinking_tokens"]
        ),
    }


class UsageVertexLLM(VertexGeminiLLM):
    """``VertexGeminiLLM`` whose replies are :class:`~mace.llm.VertexQueryResult`."""

    def _run_generate(self, user_message, tools=None):
        from google.genai import models as genai_models

        responses: list = []
        with _WRAP_LOCK:
            original = genai_models.Models.generate_content

            def recording_generate_content(models_self, *args, **kwargs):
                response = original(models_self, *args, **kwargs)
                responses.append(response)
                return response

            genai_models.Models.generate_content = recording_generate_content
            try:
                reply = super()._run_generate(user_message, tools)
            finally:
                genai_models.Models.generate_content = original
        return VertexQueryResult(
            result=reply.result,
            returncode=reply.returncode,
            stderr=reply.stderr,
            stream_result=reply.stream_result,
            success=reply.success,
            usage=usage_from_responses(self.model, responses),
        )
