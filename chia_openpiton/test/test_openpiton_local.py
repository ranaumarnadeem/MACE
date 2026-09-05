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


class TestWorkspaceFiles:
    def test_put_file_writes_under_the_root(self, node):
        path = node.put_file("piton/verif/diag/c/riscv/ariane/gate.c", "int main(){}\n")
        assert os.path.isfile(path)
        assert path.startswith(node.piton_root)

    def test_put_file_rejects_escaping_the_checkout(self, node):
        with pytest.raises(ValueError, match="escapes base dir"):
            node.put_file("../../etc/passwd", "x")

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
