"""Tier-1 test: real Ray, real cache/bypass actors, for mace.replay.

Run:
    pytest mace/test/cluster/replay_e2e_test.py -q

Mirrors chia/base/test/test_cache_tags.py's own integration tests (6 and 7
there: auto-write on a real call, then a bypass provider serving the same
tag back from cache) using mace.replay's wrappers instead of chia's
cache/bypass API directly.
"""

from __future__ import annotations

import pytest

ray = pytest.importorskip("ray")

from chia.base.ChiaFunction import ChiaFunction, get  # noqa: E402
from chia.base.bypass import Bypass  # noqa: E402
from chia.base.cache import stop_cache  # noqa: E402

from mace.replay import enable_caching, enable_replay, tag_for  # noqa: E402


@ChiaFunction()
def toy_fn(x: int) -> int:
    return x * x


CACHE_YAML = """
cache:
  toy_fn:
    cache: true
"""

BYPASS_YAML = """
bypass:
  toy_fn:
    bypass: true
"""


def _write_yaml(content: str, tmp_path, name: str) -> str:
    path = tmp_path / name
    path.write_text(content)
    return str(path)


@pytest.fixture(scope="module")
def ray_local():
    ray.init(ignore_reinit_error=True, log_to_driver=False)
    yield
    ray.shutdown()


class TestReplayRoundTrip:
    def test_real_run_caches_then_replay_serves_from_cache_not_recompute(
        self, ray_local, tmp_path
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cache_yaml = _write_yaml(CACHE_YAML, tmp_path, "cache.yaml")

        enable_caching(str(cache_dir), size=4, units="MB", yaml_path=cache_yaml)
        try:
            Bypass()  # nothing bypassed yet -- toy_fn runs for real
            tag = tag_for("run1", 0, "t", "compute")
            real = get(toy_fn.chia_remote(7, _chia_tag=tag))
            assert real == 49  # 7**2, auto-written to the cache under `tag`

            bypass_yaml = _write_yaml(BYPASS_YAML, tmp_path, "bypass.yaml")
            enable_replay(bypass_yaml, func_names=("toy_fn",))

            # A different input: if this actually recomputed, it would be
            # 998001 (999**2), not the cached 49 -- proving it was served
            # from cache, not re-run.
            replayed = get(toy_fn.chia_remote(999, _chia_tag=tag))
            assert replayed == 49
        finally:
            stop_cache()

    def test_replay_of_an_untagged_call_is_a_cache_miss(self, ray_local, tmp_path):
        cache_dir = tmp_path / "cache2"
        cache_dir.mkdir()
        cache_yaml = _write_yaml(CACHE_YAML, tmp_path, "cache2.yaml")

        enable_caching(str(cache_dir), size=4, units="MB", yaml_path=cache_yaml)
        try:
            bypass_yaml = _write_yaml(BYPASS_YAML, tmp_path, "bypass2.yaml")
            enable_replay(bypass_yaml, func_names=("toy_fn",))

            with pytest.raises(Exception, match="cache miss"):
                get(toy_fn.chia_remote(5, _chia_tag="never-written"))
        finally:
            stop_cache()
