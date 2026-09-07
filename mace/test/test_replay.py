"""Tier-0 tests for mace.replay.tag_for.

Run:
    pytest mace/test/test_replay.py -q

enable_caching/enable_replay/cache_provider are exercised end-to-end
against real Ray in mace/test/cluster/replay_e2e_test.py -- they wrap
chia.base.cache/chia.base.bypass actors, which don't exist without Ray, so
there is nothing meaningful to unit-test about them in isolation beyond
what tag_for covers here.
"""

from __future__ import annotations

from mace.replay import tag_for


class TestTagFor:
    def test_format(self):
        assert tag_for("run1", 0, "cfg1", "build") == "run1/iter0/cfg1/build"

    def test_stable_across_calls(self):
        assert tag_for("run1", 2, "t", "run") == tag_for("run1", 2, "t", "run")

    def test_distinct_inputs_give_distinct_tags(self):
        base = tag_for("run1", 0, "a", "build")
        assert base != tag_for("run2", 0, "a", "build")
        assert base != tag_for("run1", 1, "a", "build")
        assert base != tag_for("run1", 0, "b", "build")
        assert base != tag_for("run1", 0, "a", "run")
