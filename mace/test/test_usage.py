"""Tier-0 tests for mace.usage.

Run:
    pytest mace/test/test_usage.py -q
"""

from __future__ import annotations

import threading

import pytest
from chia.base.llm_call import QueryResult

from mace import usage
from mace.llm import VertexQueryResult
from mace.test.conftest import FakeLLM


def vertex_reply(success=True, **counts):
    return VertexQueryResult(
        result="ok", returncode=0, stderr="", stream_result="ok", success=success, usage=counts
    )


class TestCallFrom:
    def test_reads_tokens_and_cost_from_usage(self):
        call = usage.call_from(
            "plan",
            vertex_reply(input_tokens=10, output_tokens=2, thinking_tokens=5, cost_usd=0.5),
            1.5,
        )
        assert call == usage.LLMCall(
            phase="plan", input_tokens=10, output_tokens=2, thinking_tokens=5,
            usd=0.5, wall_s=1.5, ok=True, reply="ok",
        )

    def test_a_reply_without_usage_records_zero_tokens(self):
        query = QueryResult(result="ok", returncode=0, stderr="", stream_result="", success=True)
        call = usage.call_from("task", query, 0.1)
        assert (call.input_tokens, call.output_tokens, call.thinking_tokens, call.usd) == (0, 0, 0, 0.0)
        assert call.ok

    def test_ok_follows_the_reply_s_success(self):
        assert not usage.call_from("task", vertex_reply(success=False), 0.0).ok


class TestUsageLog:
    def test_take_returns_only_calls_added_since_the_last_take(self):
        log = usage.UsageLog()
        log.add(usage.LLMCall("plan"))
        assert [c.phase for c in log.take()] == ["plan"]
        log.add(usage.LLMCall("task"))
        log.add(usage.LLMCall("triage"))
        assert [c.phase for c in log.take()] == ["task", "triage"]
        assert log.take() == ()
        assert len(log.calls()) == 3

    def test_total_usd_sums_every_call(self):
        log = usage.UsageLog()
        log.add(usage.LLMCall("plan", usd=0.25))
        log.add(usage.LLMCall("task", usd=0.5))
        assert log.total_usd() == pytest.approx(0.75)

    def test_adds_from_many_threads_are_all_kept(self):
        log = usage.UsageLog()

        def add_many():
            for _ in range(200):
                log.add(usage.LLMCall("task"))

        threads = [threading.Thread(target=add_many) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(log.calls()) == 1600


class TestPrompt:
    def test_records_the_call_in_the_active_log(self):
        log = usage.UsageLog()
        llm = FakeLLM(responses=["reply"])
        with usage.recording(log):
            query = usage.prompt(llm, "plan", "hello")
        assert query.result == "reply"
        assert [c.phase for c in log.calls()] == ["plan"]
        assert llm.calls == [("hello", ())]

    def test_records_nothing_without_an_active_log(self):
        llm = FakeLLM(responses=["reply"])
        assert usage.prompt(llm, "plan", "hello").result == "reply"

    def test_a_raising_call_is_recorded_as_failed_and_re_raised(self):
        log = usage.UsageLog()
        llm = FakeLLM(responses=[])
        with usage.recording(log), pytest.raises(RuntimeError, match="exhausted"):
            usage.prompt(llm, "triage", "hello")
        (call,) = log.calls()
        assert call.phase == "triage"
        assert not call.ok

    def test_recording_restores_the_previous_log(self):
        outer, inner = usage.UsageLog(), usage.UsageLog()
        with usage.recording(outer):
            with usage.recording(inner):
                usage.note("task", vertex_reply(), 0.0)
            usage.note("plan", vertex_reply(), 0.0)
        assert [c.phase for c in inner.calls()] == ["task"]
        assert [c.phase for c in outer.calls()] == ["plan"]


class TestPromptRetrying:
    class Limited(Exception):
        error_type = "rate_limit"

    class Llm:
        def __init__(self, errors):
            self.errors = list(errors)
            self.calls = 0

        def prompt(self, message, tools=()):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            return QueryResult(result="ok", returncode=0, stderr="", stream_result="ok", success=True)

    @pytest.fixture
    def waits(self, monkeypatch):
        waits = []
        monkeypatch.setattr("mace.usage.time.sleep", lambda seconds: waits.append(seconds))
        return waits

    def test_a_call_that_works_waits_for_nothing(self, waits):
        llm = self.Llm([])
        assert usage.prompt_retrying(llm, "task", "hi").result == "ok"
        assert waits == [] and llm.calls == 1

    def test_it_waits_after_each_rate_limit_and_records_the_failed_tries(self, waits):
        llm = self.Llm([self.Limited("429"), self.Limited("429")])
        log = usage.UsageLog()
        with usage.recording(log):
            assert usage.prompt_retrying(llm, "task", "hi").result == "ok"
        assert waits == [30, 60] and llm.calls == 3
        assert [c.ok for c in log.calls()] == [False, False, True]

    def test_it_gives_up_after_the_last_wait(self, waits):
        llm = self.Llm([self.Limited("429")] * 9)
        with pytest.raises(self.Limited):
            usage.prompt_retrying(llm, "task", "hi")
        assert waits == [30, 60, 120] and llm.calls == 4

    def test_the_waits_can_be_changed(self, waits):
        llm = self.Llm([self.Limited("429")] * 9)
        with pytest.raises(self.Limited):
            usage.prompt_retrying(llm, "task", "hi", waits=(1,))
        assert waits == [1] and llm.calls == 2

    def test_any_other_error_is_raised_at_once(self, waits):
        llm = self.Llm([RuntimeError("boom"), self.Limited("429")])
        with pytest.raises(RuntimeError, match="boom"):
            usage.prompt_retrying(llm, "task", "hi")
        assert waits == [] and llm.calls == 1


class TestIsRateLimit:
    def test_a_backend_error_of_type_rate_limit(self):
        class Limited(Exception):
            error_type = "rate_limit"

        assert usage.is_rate_limit(Limited("429"))

    @pytest.mark.parametrize("error", [RuntimeError("x"), ValueError("rate_limit"), TimeoutError()])
    def test_any_other_error_is_not(self, error):
        assert not usage.is_rate_limit(error)

    def test_another_backend_error_type_is_not(self):
        class Auth(Exception):
            error_type = "authentication"

        assert not usage.is_rate_limit(Auth("denied"))
