"""Tier-0 tests for mace.test.conftest.FakeLLM.

Run:
    pytest mace/test/test_fake_llm.py -q

FakeLLM is test infrastructure, not application code -- these tests exist so
later loop/agent tests can trust it rather than re-proving it every time.
"""

from __future__ import annotations

import warnings

import pytest

from chia.base.llm_call import LLMCallBase, QueryResult
from mace.test.conftest import FakeLLM


class TestScriptedResponses:
    def test_returns_responses_in_order(self):
        llm = FakeLLM(responses=["first", "second", "third"])
        assert llm.prompt("a").result == "first"
        assert llm.prompt("b").result == "second"
        assert llm.prompt("c").result == "third"

    def test_bare_string_becomes_a_successful_query_result(self):
        out = FakeLLM(responses=["TASK: build hello_world.c"]).prompt("go")
        assert out.result == "TASK: build hello_world.c"
        assert out.stream_result == "TASK: build hello_world.c"
        assert out.returncode == 0
        assert out.stderr == ""
        assert out.success is True

    def test_query_result_is_returned_untouched(self):
        canned = QueryResult(result="X", returncode=7, stderr="oops", stream_result="X", success=False)
        out = FakeLLM(responses=[canned]).prompt("go")
        assert out is canned

    def test_mixed_strings_and_query_results(self):
        canned = QueryResult(result="R", returncode=0, stderr="", stream_result="R", success=True)
        llm = FakeLLM(responses=["plain", canned])
        assert llm.prompt("a").result == "plain"
        assert llm.prompt("b") is canned

    def test_exhausted_queue_raises_clearly(self):
        llm = FakeLLM(responses=["only one"])
        llm.prompt("a")
        with pytest.raises(RuntimeError, match="exhausted"):
            llm.prompt("b")

    def test_empty_script_raises_on_first_call(self):
        with pytest.raises(RuntimeError, match="exhausted"):
            FakeLLM(responses=[]).prompt("a")


class TestCallLog:
    def test_records_message_and_tools_per_call(self):
        llm = FakeLLM(responses=["r1", "r2"])
        llm.prompt("hello", tools=[])
        llm.prompt("world")
        assert llm.calls == [("hello", ()), ("world", ())]


class TestLLMCallBaseContract:
    def test_is_an_llm_call_base(self):
        assert isinstance(FakeLLM(responses=[]), LLMCallBase)

    def test_prompt_carries_the_fake_creds_resource(self):
        assert FakeLLM.prompt._chia_options["resources"] == {"fake_creds": 0.01}

    def test_prompt_exposes_the_remote_surface(self):
        assert hasattr(FakeLLM.prompt, "chia_remote")
        assert hasattr(FakeLLM.prompt, "options")

    def test_permission_args_do_not_warn(self):
        """Unlike a real backend, FakeLLM declares support for both, so passing
        them must not trigger LLMCallBase's "this backend ignores it" warning."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            FakeLLM(responses=[], dangerously_skip_permissions=True, config={"x": "y"})
