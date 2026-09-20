"""Tier-0 tests for OpenPitonWorkspaceNode: no Ray, no OpenPiton, stub `sims`.

Run:
    pytest chia_openpiton/test/test_openpiton_local.py -q

A `@ChiaFunction` called directly runs in the caller's process, and
``require_colocated=False`` skips placement-group reservation, so the whole
node surface is exercised here against a stub checkout. What these tests check
is the part we control: the exact argv handed to sims, the pass/fail rule, the
timeout contract, and that nothing touches the filesystem before validating.
"""

from __future__ import annotations

import os

import pytest

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import PitonConfig


@pytest.fixture
def node(stub_piton_root):
    """An unpinned node over the stub checkout (no placement group, no Ray)."""
    return OpenPitonWorkspaceNode(str(stub_piton_root), require_colocated=False)


@pytest.fixture
def cfg():
    """A config with the runtime-probed fields pinned, so build_id is stable."""
    return PitonConfig(
        core="sparc",  # sparc needs no RISCV/VERILATOR_ROOT env to exist
        source_rev="deadbeef",
        verilator_version="Verilator 4.014 2019-01-01",
    )


class TestRootValidation:
    """Regression tests for the bug that produced a directory named
    ``<...OpenPitonWorkspaceNode object at 0x...>`` in the repo."""

    def test_passing_a_node_where_a_path_belongs_is_rejected(self, node, cfg):
        with pytest.raises(ValueError, match="piton_root must be a path string"):
            OpenPitonWorkspaceNode.build(node, cfg)

    def test_rejection_creates_no_directories(self, tmp_path, cfg, monkeypatch):
        """The original bug ran makedirs before the failure surfaced."""
        monkeypatch.chdir(tmp_path)
        before = set(os.listdir(tmp_path))
        with pytest.raises(ValueError):
            OpenPitonWorkspaceNode.build(object(), cfg)
        assert set(os.listdir(tmp_path)) == before

    def test_nonexistent_root_is_rejected(self, cfg):
        with pytest.raises(ValueError, match="not a directory"):
            OpenPitonWorkspaceNode.build("/nope/does/not/exist", cfg)

    def test_empty_root_is_rejected(self, cfg):
        with pytest.raises(ValueError, match="must not be empty"):
            OpenPitonWorkspaceNode.build("   ", cfg)

    def test_constructor_rejects_a_bad_root_immediately(self):
        with pytest.raises(ValueError):
            OpenPitonWorkspaceNode("/nope/does/not/exist", require_colocated=False)


class TestRootOnRemoteWorker:
    """root_on_remote_worker=True: for a checkout that only exists on a
    different machine than whatever constructs the node (a real,
    multi-machine cluster, not this test's stub setup) -- see
    OpenPitonWorkspaceNode.__init__'s own docstring for why this is a
    separate flag from require_colocated rather than inferred from it."""

    def test_absolute_path_not_locally_present_is_accepted(self, tmp_path):
        missing = str(tmp_path / "not-actually-here")
        node = OpenPitonWorkspaceNode(
            missing, require_colocated=False, root_on_remote_worker=True
        )
        assert node.piton_root == missing

    def test_relative_path_is_still_rejected(self):
        with pytest.raises(ValueError, match="absolute path"):
            OpenPitonWorkspaceNode(
                "relative/path", require_colocated=False, root_on_remote_worker=True
            )

    def test_requires_require_colocated_false(self):
        """A self-reserved placement group can't promise it lands on the
        machine holding piton_root, so pairing this with the default
        require_colocated=True is a configuration mistake, not a valid
        multi-machine setup -- must fail loudly, not silently reserve the
        wrong machine."""
        with pytest.raises(ValueError, match="require_colocated=False"):
            OpenPitonWorkspaceNode("/some/remote/path", root_on_remote_worker=True)

    def test_default_still_validates_locally(self, tmp_path):
        """root_on_remote_worker defaults to False -- existing callers who
        never pass it keep the original, always-check-locally behavior."""
        with pytest.raises(ValueError, match="not a directory"):
            OpenPitonWorkspaceNode(str(tmp_path / "missing"), require_colocated=False)


class TestRootBinding:
    """Both call styles must work: bound instance members and raw class members."""

    def test_instance_member_binds_the_root(self, node, cfg, sims_argv):
        art = node.build(cfg)
        assert art.success is True
        assert len(sims_argv) == 1

    def test_class_member_still_takes_an_explicit_root(self, stub_piton_root, cfg, sims_argv):
        art = OpenPitonWorkspaceNode.build(str(stub_piton_root), cfg)
        assert art.success is True

    def test_options_keeps_the_root_bound(self, node):
        """.options(...) must return something still carrying the root."""
        handle = node.build.options(num_cpus=2)
        assert handle._root == node.piton_root

    def test_class_attribute_still_declares_its_resources(self):
        """Instance rebinding must not disturb ColocatedNode's introspection."""
        demands = OpenPitonWorkspaceNode.build._chia_options["resources"]
        assert demands == {"openpiton": 1}


class TestEnvironment:
    """The env prologue must match what OpenPiton's own CI exports."""

    def test_ariane_env_is_overridable_but_defaulted(self):
        from chia_openpiton.openpiton_workspace import _env_prefix

        env = _env_prefix("/work/openpiton", "ariane")
        assert 'export PITON_ROOT=/work/openpiton' in env
        # ARIANE_ROOT needs its trailing slash; RISCV and VERILATOR_ROOT must
        # defer to the worker's own values when set.
        assert 'ARIANE_ROOT="$PITON_ROOT/piton/design/chip/tile/ariane/"' in env
        assert '${RISCV:-' in env
        assert 'source "$PITON_ROOT/piton/piton_settings.bash"' in env

    def test_riscv_bin_is_prepended_after_sourcing_piton_settings(self):
        """piton_settings.bash prepends /usr/bin to PATH, and Ubuntu's
        riscv64-unknown-elf-gcc package ships no newlib -- so if our toolchain
        is added before the source, every diag fails on a missing string.h."""
        from chia_openpiton.openpiton_workspace import _env_prefix

        env = _env_prefix("/work/openpiton", "ariane")
        source_at = env.index("piton_settings.bash")
        riscv_path_at = env.index('export PATH="$RISCV/bin:$PATH"')
        assert riscv_path_at > source_at

    def test_verilator_root_is_only_set_when_a_real_install_exists(self):
        """Verilator finds its data files via VERILATOR_ROOT; pointing it at a
        missing or half-built tree breaks a working system Verilator."""
        from chia_openpiton.openpiton_workspace import _env_prefix

        env = _env_prefix("/work/openpiton", "ariane")
        assert '[ -x "$ARIANE_ROOT/tmp/verilator-4.014/bin/verilator" ]' in env
        assert '[ -z "${VERILATOR_ROOT:-}" ]' in env

    def test_sparc_needs_no_riscv_toolchain(self):
        from chia_openpiton.openpiton_workspace import _env_prefix

        env = _env_prefix("/work/openpiton", "sparc")
        assert "ARIANE_ROOT" not in env
        assert "VERILATOR_ROOT" not in env
        assert 'source "$PITON_ROOT/piton/piton_settings.bash"' in env

    def test_pico_needs_no_riscv_toolchain(self):
        """pico reuses the installed riscv64-unknown-elf-gcc via a sims flag
        (-rv32_target_triple, see PitonConfig.sims_flags) rather than any
        _env_prefix PATH/env-var override -- it needs exactly what sparc
        already gets: nothing beyond piton_settings.bash."""
        from chia_openpiton.openpiton_workspace import _env_prefix

        env = _env_prefix("/work/openpiton", "pico")
        assert "ARIANE_ROOT" not in env
        assert "VERILATOR_ROOT" not in env
        assert 'source "$PITON_ROOT/piton/piton_settings.bash"' in env


class TestBuildArgv:
    def test_mesh_core_and_network_reach_sims(self, node, sims_argv):
        node.build(PitonConfig(x_tiles=2, y_tiles=2, core="sparc"))
        argv = sims_argv.last()
        assert "-sys=manycore" in argv
        assert "-x_tiles=2" in argv and "-y_tiles=2" in argv
        assert "-network_config=2dmesh_config" in argv
        assert "-vlt_build" in argv

    def test_ariane_flag_present_only_for_ariane(self, node, sims_argv):
        node.build(PitonConfig(core="sparc"))
        assert "-ariane" not in sims_argv.last()

    def test_pico_flags_present_only_for_pico(self, node, sims_argv):
        node.build(PitonConfig(core="pico"))
        argv = sims_argv.last()
        assert "-pico" in argv
        assert "-rv32_target_triple=riscv64-unknown-elf" in argv
        assert "-ariane" not in argv

    def test_build_id_isolates_configurations(self, node, sims_argv):
        """Without -build_id every model would land in rel-0.1 and collide."""
        node.build(PitonConfig(x_tiles=1, core="sparc"))
        node.build(PitonConfig(x_tiles=2, core="sparc"))
        first, second = sims_argv.lines()
        assert "-build_id=mace_" in first and "-build_id=mace_" in second
        assert first != second

    def test_no_timing_added_for_verilator_5(self, node, sims_argv):
        node.build(PitonConfig(core="sparc", verilator_version="Verilator 5.049 devel"))
        assert "-vlt_build_args=--no-timing" in sims_argv.last()

    def test_no_timing_absent_for_verilator_4(self, node, sims_argv):
        """v4 has no --timing flag and errors if given one."""
        node.build(PitonConfig(core="sparc", verilator_version="Verilator 4.038 2020-07-11"))
        assert "--no-timing" not in sims_argv.last()

    def test_unknown_sim_type_rejected(self, node, cfg):
        with pytest.raises(ValueError, match="sim_type"):
            node.build(cfg, sim_type="icarus")


class TestBuildResult:
    def test_success_requires_the_model_binary(self, node, cfg):
        art = node.build(cfg)
        assert art.success is True
        assert art.binary_path.endswith("obj_dir/Vcmp_top")
        assert os.path.exists(art.binary_path)
        assert art.failure_reason == ""

    def test_failed_build_is_reported_not_raised(self, node, cfg, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_FAIL_BUILD", "1")
        art = node.build(cfg)
        assert art.success is False
        assert art.returncode != 0
        assert art.binary_path == ""
        assert art.failure_reason  # tagged, not empty

    def test_timeout_yields_returncode_minus_one(self, node, cfg, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_SLEEP", "5")
        art = node.build(cfg, timeout_seconds=1)
        assert art.returncode == -1
        assert art.success is False
        assert "timed out" in art.stderr

    def test_cache_key_is_carried_on_the_artifact(self, node, cfg):
        assert node.build(cfg).cache_key == cfg.key

    def test_non_manycore_sys_builds_under_its_own_dir_and_binary_name(self, node, cfg):
        """A unit-test sys (e.g. ifu_esl_lfsr) builds a differently-named
        binary under build/<sys>/, not build/manycore/Vcmp_top -- the
        adapter must find it via glob, not the manycore fast path."""
        import dataclasses

        ut_cfg = dataclasses.replace(cfg, sys="ifu_esl_lfsr")
        art = node.build(ut_cfg)
        assert art.success is True
        assert "/ifu_esl_lfsr/" in art.binary_path.replace("\\", "/")
        assert art.binary_path.endswith("obj_dir/Vifu_esl_lfsr_top")
        assert os.path.exists(art.binary_path)


class TestBuildReuse:
    """A build_id that already succeeded is served from disk, not rebuilt --
    this is what makes repeated agent iterations against one config cheap."""

    def test_first_build_is_not_marked_reused(self, node, cfg):
        assert node.build(cfg).reused is False

    def test_second_identical_build_skips_sims_entirely(self, node, cfg, sims_argv):
        node.build(cfg)
        assert len(sims_argv) == 1
        second = node.build(cfg)
        assert len(sims_argv) == 1  # sims was NOT invoked again
        assert second.reused is True
        assert second.success is True

    def test_reused_artifact_still_has_a_valid_binary_path(self, node, cfg):
        node.build(cfg)
        second = node.build(cfg)
        assert second.binary_path.endswith("obj_dir/Vcmp_top")
        assert os.path.exists(second.binary_path)

    def test_clean_forces_a_real_rebuild(self, node, cfg, sims_argv):
        node.build(cfg)
        again = node.build(cfg, clean=True)
        assert len(sims_argv) == 2  # sims WAS invoked the second time
        assert again.reused is False

    def test_a_different_config_is_never_served_from_the_first(self, node, sims_argv):
        a = PitonConfig(x_tiles=1, core="sparc")
        b = PitonConfig(x_tiles=2, core="sparc")
        node.build(a)
        result_b = node.build(b)
        assert len(sims_argv) == 2
        assert result_b.reused is False

    def test_a_failed_build_leaves_no_marker_to_reuse(self, node, cfg, sims_argv, monkeypatch):
        monkeypatch.setenv("FAKE_SIMS_FAIL_BUILD", "1")
        first = node.build(cfg)
        assert first.success is False
        monkeypatch.delenv("FAKE_SIMS_FAIL_BUILD")
        second = node.build(cfg)
        assert len(sims_argv) == 2  # the failed attempt must not be "reused"
        assert second.success is True
        assert second.reused is False


class TestBuildDetectsStaleAddressMap:
    """configure(address_map=...) writes straight into this checkout's one
    shared piton/verif/env/manycore -- not scoped by build_id. build() must
    refuse rather than silently compile (and permanently cache) against a
    different config's map if the checkout changed since this config was
    created -- see configure()'s and build()'s own docstrings.
    """

    def test_raises_when_the_checkout_diff_no_longer_matches(self, node, monkeypatch):
        cfg = PitonConfig(core="sparc", diff="+ old map contents")
        monkeypatch.setattr(
            OpenPitonWorkspaceNode,
            "_git",
            lambda root, args, timeout_seconds=60: "+ a different map, from a later configure() call",
        )
        with pytest.raises(ValueError, match="no longer matches"):
            node.build(cfg)

    def test_proceeds_when_the_checkout_diff_still_matches(self, node, monkeypatch):
        cfg = PitonConfig(
            core="sparc", source_rev="deadbeef", verilator_version="Verilator 4.014 2019-01-01",
            diff="+ same map contents",
        )
        monkeypatch.setattr(
            OpenPitonWorkspaceNode,
            "_git",
            lambda root, args, timeout_seconds=60: "+ same map contents",
        )
        art = node.build(cfg)
        assert art.success is True

    def test_a_config_with_no_recorded_diff_skips_the_check_entirely(self, node, cfg, monkeypatch):
        """The overwhelmingly common case (no address_map ever used): no
        _git call should even happen for this check."""

        def fail_if_called(*a, **kw):
            raise AssertionError("_git should not be called when config.diff is empty")

        monkeypatch.setattr(OpenPitonWorkspaceNode, "_git", fail_if_called)
        assert cfg.diff == ""
        art = node.build(cfg)
        assert art.success is True


class TestRunVerdicts:
    @pytest.mark.parametrize(
        "verdict,expected_success",
        [("pass", True), ("fail", False), ("timeout", False), ("maxcycles", False)],
    )
    def test_verdict_decides_success_not_exit_code(
        self, node, cfg, sims_argv, monkeypatch, verdict, expected_success
    ):
        """The stub always exits 0, exactly like a real RTL simulation."""
        monkeypatch.setenv("FAKE_SIMS_VERDICT", verdict)
        node.build(cfg)
        res = node.run(cfg, "princeton-test-test.s")
        assert res.returncode == 0
        assert res.verdict == verdict
        assert res.success is expected_success

    def test_finish_mask_defaults_to_one_digit_per_tile(self, node, sims_argv):
        cfg = PitonConfig(x_tiles=2, y_tiles=2, core="sparc")
        node.build(cfg)
        node.run(cfg, "hello_world.c")
        assert "-finish_mask=1111" in sims_argv.last()

    def test_precompiled_and_diag_root_reach_sims(self, node, cfg, sims_argv):
        node.build(cfg)
        node.run(cfg, "rv64ui-p-addi.S", precompiled=True, asm_diag_root="/work/workloads")
        argv = sims_argv.last()
        assert "-precompiled" in argv
        assert "-asm_diag_root=/work/workloads" in argv

    def test_each_run_gets_its_own_directory(self, node, cfg):
        node.build(cfg)
        a = node.run(cfg, "t.s")
        b = node.run(cfg, "t.s")
        assert a.run_dir != b.run_dir

    def test_verdict_survives_a_transcript_longer_than_the_shipped_tail(
        self, node, cfg, monkeypatch
    ):
        """A verbose multi-tile run where other tiles keep logging past the
        finishing tile's own verdict line must not push that line out of
        what gets parsed -- only out of what gets shipped back. See
        OpenPitonWorkspaceNode.run's own sim.log/status.log handling.
        """
        monkeypatch.setenv("FAKE_SIMS_VERDICT", "pass")
        monkeypatch.setenv("FAKE_SIMS_BIG_SIM_LOG", "1")
        node.build(cfg)
        res = node.run(cfg, "princeton-test-test.s")
        assert res.verdict == "pass"
        assert res.success is True
        assert len(res.sim_log_tail) <= 8000  # still shipped small, just parsed whole


class TestWorkspaceFiles:
    def test_put_file_writes_under_the_root(self, node):
        path = node.put_file("piton/verif/diag/c/riscv/ariane/gate.c", "int main(){}\n")
        assert os.path.isfile(path)
        assert path.startswith(node.piton_root)

    def test_put_file_rejects_escaping_the_checkout(self, node):
        with pytest.raises(ValueError, match="escapes base dir"):
            node.put_file("../../etc/passwd", "x")

    def test_collect_pattern_cannot_escape_base_dir(self, node, stub_piton_root):
        """A glob pattern with `..` components must not read files outside
        base_dir -- base_dir is a confinement boundary, not just where the
        glob starts. Reachable today from an LLM-facing tool call
        (chia_openpiton.tools.CheckoutTools.collect's `pattern` argument).
        """
        secret = stub_piton_root / "secret.txt"
        secret.write_text("do not ship this")
        run_dir = stub_piton_root / "build" / "runs" / "1"
        run_dir.mkdir(parents=True)
        (run_dir / "sim.log").write_text("ok")

        got = node.collect(str(run_dir), ("../../../secret.txt",))

        assert got.files == {}
        assert "secret.txt" not in got.files
        assert not any("secret" in name for name in got.listing)

    def test_collect_caps_large_files(self, node, stub_piton_root):
        target = stub_piton_root / "build" / "big.log"
        target.write_text("x" * 5000)
        (stub_piton_root / "build" / "small.log").write_text("ok")
        got = node.collect(str(stub_piton_root / "build"), ("*.log",), max_bytes_per_file=1000)
        assert "small.log" in got.files
        assert got.skipped["big.log"] == 5000
        assert got.listing["big.log"] == 5000

    def test_clean_removes_only_this_configs_model(self, node, cfg):
        art = node.build(cfg)
        assert os.path.isdir(art.model_dir)
        assert node.clean(cfg) is True
        assert not os.path.isdir(art.model_dir)
        assert node.clean(cfg) is False


def test_pyhp_side_effect_is_visible(node, cfg, stub_piton_root):
    """Documents why parallel builds need separate checkouts.

    pyHP writes generated .tmp.v files back into the SOURCE tree on every
    build, so two builds sharing one checkout race over the same files.
    """
    node.build(cfg)
    generated = stub_piton_root / "piton" / "verif" / "env" / "manycore" / "pc_cmp.tmp.v"
    assert generated.exists()
    assert cfg.build_id in generated.read_text()
