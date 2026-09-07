"""Tier-0 tests for mace.llm.make_llm.

Run:
    pytest mace/test/test_llm.py -q

Backends are real chia.models classes -- these tests only prove
construction picks the right class with the right knobs, never call
.prompt() (that would need real credentials/CLIs).
"""

from __future__ import annotations

import pytest

from chia.base.llm_call import LLMCallBase
from mace.llm import UnknownLLMBackendError, make_llm


class TestBackendSelection:
    def test_defaults_to_opencode(self, monkeypatch):
        monkeypatch.delenv("MACE_LLM", raising=False)
        from chia.models.opencode import OpenCodeLLM

        assert isinstance(make_llm(), OpenCodeLLM)

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

    def test_vertex_needs_a_model(self, monkeypatch):
        monkeypatch.delenv("MACE_LLM_MODEL", raising=False)
        with pytest.raises(TypeError):
            make_llm("vertex")


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
