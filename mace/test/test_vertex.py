"""Tier-0 tests for mace.vertex.

Run:
    pytest mace/test/test_vertex.py -q

No network: google-genai's generate_content and CHIA's own _run_generate
are both replaced, so these check only how UsageVertexLLM counts tokens
and puts them on the reply.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from chia.base.llm_call import QueryResult
from chia.models.vertex import VertexGeminiLLM
from google.genai import models as genai_models

from mace.llm import VertexQueryResult, make_llm, vertex_cost_usd
from mace.vertex import UsageVertexLLM, usage_from_responses


def response(prompt=0, candidates=0, thoughts=0):
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt, candidates_token_count=candidates, thoughts_token_count=thoughts
        )
    )


class TestUsageFromResponses:
    def test_sums_every_turn(self):
        usage = usage_from_responses(
            "gemini-2.5-flash", [response(100, 10, 50), response(200, 20, 0)]
        )
        assert usage["input_tokens"] == 300
        assert usage["output_tokens"] == 30
        assert usage["thinking_tokens"] == 50
        assert usage["model"] == "gemini-2.5-flash"
        assert usage["cost_usd"] == pytest.approx(vertex_cost_usd("gemini-2.5-flash", 300, 30, 50))

    def test_missing_metadata_and_none_fields_count_as_zero(self):
        none_fields = SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=None, candidates_token_count=7, thoughts_token_count=None
            )
        )
        usage = usage_from_responses("m", [SimpleNamespace(), none_fields])
        assert (usage["input_tokens"], usage["output_tokens"], usage["thinking_tokens"]) == (0, 7, 0)


class TestUsageVertexLLM:
    @pytest.fixture
    def llm(self):
        return UsageVertexLLM(model="gemini-2.5-flash", project="test-project")

    def test_make_llm_builds_it_for_vertex(self):
        llm = make_llm("vertex", model="gemini-2.5-flash", project="test-project")
        assert isinstance(llm, UsageVertexLLM)
        assert isinstance(llm, VertexGeminiLLM)

    def test_the_reply_carries_every_turn_s_tokens(self, llm, monkeypatch):
        turns = iter([response(100, 10, 40), response(150, 5, 0)])
        monkeypatch.setattr(
            genai_models.Models, "generate_content", lambda self, **kwargs: next(turns)
        )

        def two_turn_generate(self, user_message, tools=None):
            genai_models.Models.generate_content(None, model=self.model, contents=user_message)
            genai_models.Models.generate_content(None, model=self.model, contents=user_message)
            return QueryResult(result="done", returncode=0, stderr="", stream_result="log")

        monkeypatch.setattr(VertexGeminiLLM, "_run_generate", two_turn_generate)
        reply = llm._run_generate("hello")

        assert isinstance(reply, VertexQueryResult)
        assert (reply.result, reply.stream_result) == ("done", "log")
        assert reply.usage["input_tokens"] == 250
        assert reply.usage["output_tokens"] == 15
        assert reply.usage["thinking_tokens"] == 40

    def captured_configs(self, llm, monkeypatch, budget):
        """The config of each generate_content request made while *budget* is in force."""
        from google.genai import types

        from mace.llm import thinking_budget

        seen = []

        def fake_generate_content(self, **kwargs):
            seen.append(kwargs.get("config"))
            return response(1, 1, 1)

        def one_request(self, user_message, tools=None):
            config = types.GenerateContentConfig(max_output_tokens=100)
            genai_models.Models.generate_content(None, model=self.model, contents=user_message, config=config)
            return QueryResult(result="", returncode=0, stderr="", stream_result="")

        monkeypatch.setattr(genai_models.Models, "generate_content", fake_generate_content)
        monkeypatch.setattr(VertexGeminiLLM, "_run_generate", one_request)
        with thinking_budget(budget):
            llm._run_generate("hello")
        return seen

    def test_a_thinking_cap_goes_on_the_request(self, llm, monkeypatch):
        (config,) = self.captured_configs(llm, monkeypatch, 256)
        assert config.thinking_config.thinking_budget == 256
        assert config.max_output_tokens == 100  # the rest of CHIA's config is kept

    def test_a_cap_of_zero_is_sent_and_not_mistaken_for_no_cap(self, llm, monkeypatch):
        (config,) = self.captured_configs(llm, monkeypatch, 0)
        assert config.thinking_config.thinking_budget == 0

    def test_without_a_cap_the_request_is_untouched(self, llm, monkeypatch):
        (config,) = self.captured_configs(llm, monkeypatch, None)
        assert config.thinking_config is None

    def test_the_callers_config_is_not_changed(self, llm, monkeypatch):
        from google.genai import types

        original = types.GenerateContentConfig(max_output_tokens=100)

        def one_request(self, user_message, tools=None):
            genai_models.Models.generate_content(None, model=self.model, contents=user_message, config=original)
            return QueryResult(result="", returncode=0, stderr="", stream_result="")

        monkeypatch.setattr(genai_models.Models, "generate_content", lambda self, **kwargs: response(1, 1, 1))
        monkeypatch.setattr(VertexGeminiLLM, "_run_generate", one_request)
        from mace.llm import thinking_budget

        with thinking_budget(64):
            llm._run_generate("hello")
        assert original.thinking_config is None

    def test_generate_content_is_restored_after_the_call(self, llm, monkeypatch):
        def fake_generate_content(self, **kwargs):
            return response(1, 1, 1)

        monkeypatch.setattr(genai_models.Models, "generate_content", fake_generate_content)
        monkeypatch.setattr(
            VertexGeminiLLM,
            "_run_generate",
            lambda self, user_message, tools=None: QueryResult(
                result="", returncode=0, stderr="", stream_result=""
            ),
        )
        llm._run_generate("hello")
        assert genai_models.Models.generate_content is fake_generate_content

    def test_generate_content_is_restored_when_the_call_raises(self, llm, monkeypatch):
        def fake_generate_content(self, **kwargs):
            return response()

        def failing_generate(self, user_message, tools=None):
            raise RuntimeError("quota")

        monkeypatch.setattr(genai_models.Models, "generate_content", fake_generate_content)
        monkeypatch.setattr(VertexGeminiLLM, "_run_generate", failing_generate)
        with pytest.raises(RuntimeError, match="quota"):
            llm._run_generate("hello")
        assert genai_models.Models.generate_content is fake_generate_content
