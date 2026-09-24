"""mace.replay -- cheap re-examination of a past run, via CHIA's cache/bypass.

What this solves: re-running a MACE loop costs real LLM tokens and real
simulator time. Tagging a run's remote ChiaFunction calls (see tag_for())
and recording them through chia.base.cache.start_cache lets a later replay
pass re-resolve those SAME calls from disk instead of re-invoking anything
-- useful for re-inspecting a run's decisions (what did the Planner
produce, what verdict did each build/run reach) without spending anything
again.

What this does NOT solve -- read this before wiring anything to it: CHIA's
cache/bypass mechanism (chia.base.cache, chia.base.bypass) only ever
captures a ChiaFunction's RETURN VALUE, and only for calls made through
``.chia_remote(...)`` (a plain/local call, which is how
mace.loop.run_mace_step calls things, never passes through this machinery
at all -- only mace.integrator.integrate_parallel's remote dispatch can be
tagged). It has no hook into side effects: an LLM prompt call that edited
RTL through a BashTool made that edit as a side effect of the real call,
and replaying a cached QueryResult does not re-apply it. This module lets a
loop replay its own DECISIONS (LLM query text, build/run verdicts) cheaply;
it does not, and structurally cannot, replay the source-tree mutations an
agent made. That remains genuinely open -- see the design plan's Phase 1
retrospective. Reconstructing exact source state after the fact is git's
job (the integrator's applied diffs), not replay's.

Practical consequence for what to tag: only tag calls whose entire
contribution to the loop's decisions is captured in their return value --
an LLM's ``prompt`` (its QueryResult text, not what it did with its tools)
and OpenPitonWorkspaceNode's ``build``/``run`` (their artifacts, which
already fully determine pass/fail). Don't tag anything a later task's
correctness depends on beyond that return value.

YAML shape (chia's own convention -- see chia/base/test/bypass_test_all.yaml
for the closest real example; no file combining both sections exists
upstream yet). CHIA matches each key against the called function's
``__name__``, so the keys are bare names -- ``prompt``, ``build``, ``run``
-- never ``llm.prompt``:

    cache:
      prompt:
        cache: true
    bypass:
      prompt:
        bypass: true

A single ``Bypass`` instance is a process-global singleton (chia's own
design, not this module's) -- constructing a second one anywhere replaces
whichever one another part of the process was still using.
"""

from __future__ import annotations

from typing import Any

import ray

from chia.base.bypass import Bypass
from chia.base.cache import get_active_cache, start_cache


def tag_for(run_id: str, iteration: int, task_id: str, phase: str) -> str:
    """The ``_chia_tag`` for one call: stable across a real run and its
    replay, so the same call always resolves to the same cache entry."""
    return f"{run_id}/iter{iteration}/{task_id}/{phase}"


def enable_caching(
    cache_dir_path: str, *, size: float = 8, units: str = "GB", yaml_path: str | None = None
):
    """Start (or reattach to) the cache actor every tagged, cache-enabled
    call writes its return value through. Idempotent -- see
    chia.base.cache.start_cache."""
    return start_cache(size, cache_dir_path, units=units, yaml_path=yaml_path)


def cache_provider(tag: str, data_path: str, *args, **kwargs) -> Any:
    """A bypass provider that serves a tagged call's result from the active
    cache -- the standard read-side pattern (mirrors chia.base.cache's own
    module docstring). Raises KeyError on a miss, which surfaces as the
    call failing rather than silently returning something wrong.
    """
    hit, value = ray.get(get_active_cache().read.remote(tag))
    if not hit:
        raise KeyError(f"cache miss for tag {tag!r}")
    return value


def enable_replay(yaml_path: str, func_names: tuple[str, ...]) -> Bypass:
    """A Bypass wired to serve every name in *func_names* from the cache.

    Only registers the provider -- *yaml_path* still decides, per name,
    whether a call is actually bypassed (``bypass: true``) versus run for
    real. Replaces the process's previously-active Bypass, if any.
    """
    bypass = Bypass(yaml_path=yaml_path)
    for name in func_names:
        bypass.set_provider(name, cache_provider)
    return bypass
