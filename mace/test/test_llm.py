"""Tier-0 tests for mace.llm.make_llm.

Run:
    pytest mace/test/test_llm.py -q

Backends are real chia.models classes -- these tests only prove
construction picks the right class with the right knobs, never call
.prompt() (that would need real credentials/CLIs).
"""

from __future__ import annotations

import pytest

from chia.base.llm_call import LLMCallBase, QueryResult
from mace.llm import (
    DEFAULT_VERTEX_MODEL,
    UnknownLLMBackendError,
    default_model_for_backend,
    extract_cost_usd,
    make_llm,
)


class TestBackendSelection:
    def test_defaults_to_vertex(self, monkeypatch):
        """The same default as every CLI and example driver."""
        monkeypatch.delenv("MACE_LLM", raising=False)
        from chia.models.vertex import VertexGeminiLLM

        assert isinstance(make_llm(), VertexGeminiLLM)

    def test_env_var_selects_backend(self, monkeypatch):
        monkeypatch.setenv("MACE_LLM", "claude")
        from chia.models.claude import ClaudeCodeLLM

        assert isinstance(make_llm(), ClaudeCodeLLM)

    def test_explicit_backend_overrides_env_var(self, monkeypatch):
        monkeypatch.setenv("MACE_LLM", "claude")
        from chia.models.opencode import OpenCodeLLM

        assert isinstance(make_llm("opencode"), OpenCodeLLM)

    def test_backend_name_is_case_insensitive(self, monkeypatch):
        monkeypatch.delenv("MACE_LLM", raising=False)
        from chia.models.opencode import OpenCodeLLM

        assert isinstance(make_llm("OpenCode"), OpenCodeLLM)

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.delenv("MACE_LLM", raising=False)
        with pytest.raises(UnknownLLMBackendError, match="opencode"):
            make_llm("chatgpt")


class TestModelSelection:
    def test_env_var_sets_the_model(self, monkeypatch):
        monkeypatch.delenv("MACE_LLM", raising=False)
        monkeypatch.setenv("MACE_LLM_MODEL", "big-pickle")
        assert make_llm("opencode").model == "big-pickle"

    def test_explicit_model_override_wins_over_env_var(self, monkeypatch):
        monkeypatch.setenv("MACE_LLM_MODEL", "big-pickle")
        assert make_llm("opencode", model="small-pickle").model == "small-pickle"

    def test_vertex_falls_back_to_the_default_model(self, monkeypatch):
        monkeypatch.delenv("MACE_LLM_MODEL", raising=False)
        assert make_llm("vertex").model == DEFAULT_VERTEX_MODEL

    def test_env_var_model_wins_over_the_vertex_default(self, monkeypatch):
        monkeypatch.setenv("MACE_LLM_MODEL", "gemini-2.5-pro")
        assert make_llm("vertex").model == "gemini-2.5-pro"


class TestDefaultModelForBackend:
    """A zero-flag vertex default must never leak onto another backend --
    see the function's own docstring for the regression this guards.
    """

    def test_no_model_defaults_to_the_funded_model_for_vertex(self):
        assert default_model_for_backend(None, "vertex") == DEFAULT_VERTEX_MODEL

    @pytest.mark.parametrize("backend", ["opencode", "claude", "antigravity"])
    def test_no_model_stays_none_for_every_other_backend(self, backend):
        assert default_model_for_backend(None, backend) is None

    def test_explicit_model_is_never_overridden(self):
        assert default_model_for_backend("small-pickle", "vertex") == "small-pickle"
        assert default_model_for_backend("small-pickle", "opencode") == "small-pickle"


class TestEachBackendConstructs:
    """Every backend is a real LLMCallBase with a distinct creds resource."""

    @pytest.mark.parametrize(
        "backend,resource",
        [
            ("opencode", "opencode_creds"),
            ("claude", "claude_creds"),
            ("antigravity", "antigravity_creds"),
        ],
    )
    def test_backend_is_a_real_llm_call_base(self, backend, resource):
        llm = make_llm(backend)
        assert isinstance(llm, LLMCallBase)
        assert type(llm).prompt._chia_options["resources"] == {resource: 0.01}

    def test_vertex_is_a_real_llm_call_base(self):
        llm = make_llm("vertex", model="gemini-2.5-pro")
        assert isinstance(llm, LLMCallBase)
        assert type(llm).prompt._chia_options["resources"] == {"vertex_creds": 0.01}


class TestExtractCostUsd:
    def test_reads_cost_from_a_usage_dict(self):
        from chia.models.opencode import OpenCodeQueryResult

        query = OpenCodeQueryResult(
            result="ok", returncode=0, stderr="", stream_result="ok", success=True,
            usage={"cost_usd": 0.0042, "input_tokens": 100},
        )
        assert extract_cost_usd(query) == 0.0042

    def test_missing_usage_attribute_returns_zero(self):
        query = QueryResult(result="ok", returncode=0, stderr="", stream_result="ok", success=True)
        assert extract_cost_usd(query) == 0.0

    def test_none_usage_returns_zero(self):
        from chia.models.opencode import OpenCodeQueryResult

        query = OpenCodeQueryResult(
            result="ok", returncode=0, stderr="", stream_result="ok", success=True, usage=None,
        )
        assert extract_cost_usd(query) == 0.0

    def test_usage_without_cost_key_returns_zero(self):
        from chia.models.opencode import OpenCodeQueryResult

        query = OpenCodeQueryResult(
            result="ok", returncode=0, stderr="", stream_result="ok", success=True,
            usage={"input_tokens": 100},
        )
        assert extract_cost_usd(query) == 0.0

    def test_fake_llm_responses_have_no_usage_and_cost_zero(self):
        from mace.test.conftest import FakeLLM

        query = FakeLLM(responses=["hi"]).prompt("hi")
        assert extract_cost_usd(query) == 0.0
