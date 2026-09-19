"""Tier-0 tests for chia_openpiton.state_def.

Run:
    pytest chia_openpiton/test/test_state_def.py -q

The cache key is load-bearing: it names the model directory (-build_id), so a
key that collides across configurations makes two different SoCs overwrite each
other's model, and a key that changes spuriously throws away every cached build.
"""

from __future__ import annotations

import dataclasses

import pytest

from chia_openpiton.state_def import (
    DEFAULT_CACHES,
    MAX_TILES_PER_AXIS,
    PitonConfig,
    PitonRunResult,
)


class TestValidation:
    def test_defaults_are_a_valid_1x1_ariane(self):
        cfg = PitonConfig()
        assert (cfg.core, cfg.x_tiles, cfg.y_tiles) == ("ariane", 1, 1)
        assert cfg.num_tiles == 1

    @pytest.mark.parametrize("bad", [0, -1, MAX_TILES_PER_AXIS + 1])
    def test_tile_count_bounds(self, bad):
        """sims itself dies with 'x_tiles can be at most 256'; fail earlier."""
        with pytest.raises(ValueError, match="x_tiles"):
            PitonConfig(x_tiles=bad)

    def test_bool_is_not_accepted_as_a_tile_count(self):
        with pytest.raises(ValueError, match="x_tiles"):
            PitonConfig(x_tiles=True)

    def test_unknown_core_rejected(self):
        with pytest.raises(ValueError, match="core"):
            PitonConfig(core="mips")

    def test_network_config_must_be_one_sims_accepts(self):
        """Unset makes sims default to '2d_mesh', a spelling pyhplib ignores."""
        with pytest.raises(ValueError, match="network_config"):
            PitonConfig(network_config="2d_mesh")

    def test_unknown_cache_name_rejected(self):
        with pytest.raises(ValueError, match="unknown cache"):
            PitonConfig(caches={"l3": (1024, 4)})

    def test_nonpositive_cache_geometry_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            PitonConfig(caches={"l2": (0, 4)})


class TestCacheKey:
    def test_key_is_stable_across_equal_configs(self):
        assert PitonConfig().key == PitonConfig().key

    def test_key_is_independent_of_dict_insertion_order(self):
        a = PitonConfig(caches={"l1i": (16384, 4), "l2": (65536, 4)})
        b = PitonConfig(caches={"l2": (65536, 4), "l1i": (16384, 4)})
        assert a.key == b.key

    @pytest.mark.parametrize(
        "change",
        [
            {"sys": "ifu_esl_lfsr"},
            {"x_tiles": 2},
            {"y_tiles": 2},
            {"core": "sparc"},
            {"network_config": "xbar_config"},
            {"config_rtl": ("MINIMAL_MONITORING", "PITON_EXTRA")},
            {"caches": {**DEFAULT_CACHES, "l2": (131072, 4)}},
            {"extra_flags": ("-some_flag",)},
            {"source_rev": "abc123"},
            {"ariane_rev": "def456"},
            {"verilator_version": "4.014"},
            {"diff": "--- a/x\n+++ b/x\n"},
        ],
    )
    def test_every_identity_field_changes_the_key(self, change):
        """Anything that changes the produced model must change the key."""
        assert PitonConfig(**change).key != PitonConfig().key

    def test_build_id_is_derived_and_filesystem_safe(self):
        build_id = PitonConfig().build_id
        assert build_id.startswith("mace_")
        assert build_id[5:].isalnum() and len(build_id) == 17

    def test_distinct_configs_get_distinct_model_dirs(self):
        """The bug this prevents: sims defaults every model to rel-0.1."""
        assert PitonConfig(x_tiles=1).build_id != PitonConfig(x_tiles=2).build_id


class TestSimsFlags:
    def test_mesh_core_and_network_always_present(self):
        flags = PitonConfig(x_tiles=2, y_tiles=2).sims_flags()
        assert "-sys=manycore" in flags
        assert "-x_tiles=2" in flags
        assert "-y_tiles=2" in flags
        assert "-ariane" in flags
        assert "-network_config=2dmesh_config" in flags

    def test_sparc_does_not_pass_the_ariane_flag(self):
        """-ariane and the default SPARC path are mutually exclusive in sims."""
        assert "-ariane" not in PitonConfig(core="sparc").sims_flags()

    def test_pico_flag_and_target_triple_present_only_for_pico(self):
        flags = PitonConfig(core="pico").sims_flags()
        assert "-pico" in flags
        assert "-ariane" not in flags
        assert "-rv32_target_triple=riscv64-unknown-elf" in flags
        assert "-rv32_target_triple=riscv64-unknown-elf" not in PitonConfig(core="ariane").sims_flags()

    def test_config_rtl_becomes_one_flag_per_define(self):
        flags = PitonConfig(config_rtl=("MINIMAL_MONITORING", "PITON_X")).sims_flags()
        assert "-config_rtl=MINIMAL_MONITORING" in flags
        assert "-config_rtl=PITON_X" in flags

    def test_cache_geometry_expands_to_size_and_associativity(self):
        flags = PitonConfig().sims_flags()
        assert "-config_l2_size=65536" in flags
        assert "-config_l2_associativity=4" in flags

    def test_flag_order_is_deterministic(self):
        assert PitonConfig().sims_flags() == PitonConfig().sims_flags()

    def test_extra_flags_are_appended_verbatim(self):
        assert PitonConfig(extra_flags=("-uart_dmw",)).sims_flags()[-1] == "-uart_dmw"

    def test_non_manycore_sys_emits_only_sys_and_extra_flags(self):
        """A unit-test sys (e.g. ifu_esl_lfsr) has its own -toplevel=/-flist=
        in its own sims.config entry -- mesh/core/cache flags are manycore
        concepts that don't apply and shouldn't be sent."""
        flags = PitonConfig(sys="ifu_esl_lfsr", extra_flags=("-some_flag",)).sims_flags()
        assert flags == ("-sys=ifu_esl_lfsr", "-some_flag")

    def test_manycore_is_still_the_default_sys(self):
        assert PitonConfig().sims_flags()[0] == "-sys=manycore"


class TestFinishMask:
    @pytest.mark.parametrize("x,y,expected", [(1, 1, "1"), (2, 1, "11"), (4, 4, "1" * 16)])
    def test_one_digit_per_tile(self, x, y, expected):
        """A multi-tile run only passes when every hart hits the good trap."""
        assert PitonConfig(x_tiles=x, y_tiles=y).finish_mask == expected


class TestRunVerdictRule:
    def test_pass_requires_the_transcript_verdict(self):
        assert PitonRunResult.decide(returncode=0, verdict="pass") is True

    @pytest.mark.parametrize("verdict", ["fail", "timeout", "maxcycles", None])
    def test_exit_zero_is_not_enough(self, verdict):
        """An RTL sim exits 0 whether or not the program passed."""
        assert PitonRunResult.decide(returncode=0, verdict=verdict) is False

    def test_timeout_returncode_never_passes(self):
        assert PitonRunResult.decide(returncode=-1, verdict="pass") is False


def test_config_is_immutable():
    """Configs are cache keys; a mutated one would silently alias a model dir."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        PitonConfig().x_tiles = 4
