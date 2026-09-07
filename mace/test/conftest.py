"""Shared fixtures for the mace tier-0 tests.

Run:
    conda activate chia_env && pytest mace/test -q

Tier 0 needs no Ray, no LLM and no OpenPiton checkout: FakeLLM stands in for
any real chia.base.llm_call.LLMCallBase backend, and a `@ChiaFunction` called
directly runs in the caller's process.
"""

from __future__ import annotations

from collections import deque
from typing import List, Optional, Sequence, Union

import pytest

from chia.base.ChiaFunction import ChiaFunction
from chia.base.llm_call import LLMCallBase, QueryResult
from chia.base.tools.ChiaTool import ChiaTool


@pytest.fixture(scope="session", autouse=True)
def _disabled_profiler():
    """Keep CHIA's profiler singleton disabled for the whole session.

    A local `@ChiaFunction` call runs ``get_profiler()``, and the profiler's
    first construction calls ``ray.get_actor`` -- which makes Ray auto-init and
    try to join whatever cluster address happens to be lying around. Building
    the singleton while ``get_collector`` returns None pins it as disabled.
    (Same fixture as chia_openpiton/test/conftest.py: conftest fixtures do not
    cross sibling test directories, and there is no root-level conftest.py to
    share it through, so it is duplicated rather than imported.)
    """
    import chia.trace.profiler as profiler_mod

    original = profiler_mod.get_collector
    profiler_mod.get_collector = lambda namespace=None: None
    try:
        profiler_mod.reset_profiler()
        profiler_mod.get_profiler()
    finally:
        profiler_mod.get_collector = original
    yield
    profiler_mod.reset_profiler()


def _as_query_result(item: Union[QueryResult, str]) -> QueryResult:
    """Bare strings are the common case: most tests only care about the footer
    text a Planner/agent step would parse, not returncode/stderr/success."""
    if isinstance(item, QueryResult):
        return item
    return QueryResult(result=item, returncode=0, stderr="", stream_result=item, success=True)


class FakeLLM(LLMCallBase):
    """A scripted LLMCallBase: no network, no cost, no nondeterminism.

    Construct with the exact sequence of responses ``.prompt()`` should
    return, in call order (a bare string is wrapped into a successful
    ``QueryResult``; pass a ``QueryResult`` directly to control returncode,
    stderr or success too). Each call pops the next one.

    A test that scripts fewer responses than the code under test actually
    requests is the test's own bug, so running out raises immediately rather
    than hanging like a real backend waiting on a rate limit, or silently
    repeating the last response and hiding a fan-out bug behind a green test.
    """

    supports_dangerously_skip_permissions = True
    supports_config = True

    def __init__(
        self,
        responses: Sequence[Union[QueryResult, str]],
        system_message: str = "",
        **kwargs,
    ):
        super().__init__(system_message=system_message, **kwargs)
        self._queue: deque[QueryResult] = deque(_as_query_result(r) for r in responses)
        self.calls: list[tuple[str, tuple]] = []

    @ChiaFunction(resources={"fake_creds": 0.01})
    def prompt(self, user_message: str, tools: Optional[List[ChiaTool]] = []) -> QueryResult:
        self.calls.append((user_message, tuple(tools or ())))
        if not self._queue:
            raise RuntimeError(
                f"FakeLLM exhausted: prompt() call #{len(self.calls)} has no "
                f"scripted response left (message: {user_message[:80]!r})"
            )
        return self._queue.popleft()
