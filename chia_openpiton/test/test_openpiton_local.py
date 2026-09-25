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


class TestVerilatorVersionText:
    """Memoized per (root, core): the real mace loop never calls configure()
    (the only path that would otherwise cache this on a PitonConfig), so
    build() re-derives it on every non-cache-hit build -- not worth a fresh
    subprocess spawn every time. See the method's own docstring.
    """

    def test_memoizes_per_root_and_core(self, monkeypatch, tmp_path):
        import chia_openpiton.openpiton_workspace as ws

        monkeypatch.setattr(ws, "_VERILATOR_VERSION_CACHE", {})
        calls = []

        def fake_run(command, root, core, cwd, timeout_seconds, env=None):
            calls.append((command, root, core))
            return ("Verilator 5.052 2024-01-01\n", "", 0, 0.01)

        monkeypatch.setattr(ws, "_run", fake_run)

        first = ws.OpenPitonWorkspaceNode.verilator_version_text(str(tmp_path), "ariane")
        second = ws.OpenPitonWorkspaceNode.verilator_version_text(str(tmp_path), "ariane")

        assert first == second == "Verilator 5.052 2024-01-01\n"
        assert len(calls) == 1  # the second call was served from the cache

    def test_different_core_is_not_served_from_the_same_cache_entry(self, monkeypatch, tmp_path):
        import chia_openpiton.openpiton_workspace as ws

        monkeypatch.setattr(ws, "_VERILATOR_VERSION_CACHE", {})
        seen_cores = []

        def fake_run(command, root, core, cwd, timeout_seconds, env=None):
            seen_cores.append(core)
            return (f"Verilator for {core}\n", "", 0, 0.01)

        monkeypatch.setattr(ws, "_run", fake_run)

        ws.OpenPitonWorkspaceNode.verilator_version_text(str(tmp_path), "ariane")
        ws.OpenPitonWorkspaceNode.verilator_version_text(str(tmp_path), "sparc")

        assert seen_cores == ["ariane", "sparc"]

    def test_a_failed_lookup_is_not_cached(self, monkeypatch, tmp_path):
        import chia_openpiton.openpiton_workspace as ws

        monkeypatch.setattr(ws, "_VERILATOR_VERSION_CACHE", {})
        calls = []

        def fake_run(command, root, core, cwd, timeout_seconds, env=None):
            calls.append(1)
            return ("", "verilator: command not found", 127, 0.01)

        monkeypatch.setattr(ws, "_run", fake_run)

        ws.OpenPitonWorkspaceNode.verilator_version_text(str(tmp_path), "ariane")
        ws.OpenPitonWorkspaceNode.verilator_version_text(str(tmp_path), "ariane")

        assert len(calls) == 2  # retried both times, nothing bad cached


class TestConfigure:
    def test_probes_run_concurrently_and_populate_the_config(self, node, monkeypatch):
        """configure()'s four probes (two git rev-parses, a git diff, a
        verilator --version) are independent -- dispatched concurrently so
        this costs close to the slowest single one, not the sum of all
        four. See configure()'s own comment.
        """
        import time as time_module

        from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

        delay = 0.15

        def slow_git(root, args, timeout_seconds=60):
            time_module.sleep(delay)
            return "-".join(args)[:20]

        def slow_version(root, core="ariane", timeout_seconds=120):
            time_module.sleep(delay)
            return "Verilator 5.052"

        monkeypatch.setattr(OpenPitonWorkspaceNode, "_git", slow_git)
        monkeypatch.setattr(OpenPitonWorkspaceNode, "verilator_version_text", slow_version)

        started = time_module.monotonic()
        cfg = node.configure(core="sparc")
        elapsed = time_module.monotonic() - started

        assert cfg.verilator_version == "Verilator 5.052"
        assert cfg.source_rev  # populated from the (fake) git call
        assert cfg.ariane_rev
        assert cfg.diff
        # Sequential would take ~4*delay; concurrent should be close to ~delay.
        assert elapsed < delay * 2.5


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

    def test_keyboard_interrupt_kills_the_process_group_and_propagates(self, node, cfg, monkeypatch):
        """Ctrl-C during a build must not leave sims/Verilator running
        orphaned in the background: start_new_session means the child is in
        its own process group and never sees the terminal's own SIGINT, so
        _run must kill it itself -- see _run's own docstring.
        """
        import subprocess as subprocess_module

        killed_pids = []
        real_communicate = subprocess_module.Popen.communicate

        def fake_communicate(self, *a, **kw):
            # build() runs git queries (the address-map recheck and the
            # source fingerprint) before sims; only the sims call is the one
            # this test means to interrupt.
            if "sims " in str(self.args):
                raise KeyboardInterrupt()
            return real_communicate(self, *a, **kw)

        monkeypatch.setattr(subprocess_module.Popen, "communicate", fake_communicate)
        monkeypatch.setattr(
            "chia_openpiton.openpiton_workspace.os.killpg",
            lambda pid, sig: killed_pids.append(pid),
        )

        with pytest.raises(KeyboardInterrupt):
            node.build(cfg)

        assert killed_pids  # the process group was actually targeted for a kill

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


class TestBuildReuseChecksTheCheckout:
    """The build ID covers the configuration, so a patch, an RTL edit, or a
    new commit leaves it unchanged. The source fingerprint in the build
    marker is what makes such a change rebuild the model instead of serving
    the one built before it. The config here is constructed directly, as
    mace's loop builds every task's, so build() does no address-map recheck.
    """

    @pytest.fixture
    def loop_cfg(self):
        return PitonConfig(core="sparc", verilator_version="Verilator 4.014 2019-01-01")

    @pytest.fixture
    def checkout(self, monkeypatch):
        """The git state build() sees, editable between builds."""
        state = {"head": "c0ffee", "diff": "", "untracked": ""}

        def fake_git(root, args, timeout_seconds=60):
            return {"rev-parse": state["head"], "diff": state["diff"], "ls-files": state["untracked"]}.get(
                args[0], ""
            )

        monkeypatch.setattr(OpenPitonWorkspaceNode, "_git", fake_git)
        return state

    def test_an_unchanged_checkout_reuses_the_build(self, node, loop_cfg, checkout, sims_argv):
        node.build(loop_cfg)
        second = node.build(loop_cfg)
        assert len(sims_argv) == 1
        assert second.reused is True

    def test_a_tracked_edit_rebuilds(self, node, loop_cfg, checkout, sims_argv):
        node.build(loop_cfg)
        checkout["diff"] = "+ patch_openpiton.sh fix 10"
        second = node.build(loop_cfg)
        assert len(sims_argv) == 2
        assert second.reused is False
        assert second.success is True
        assert node.build(loop_cfg).reused is True  # the rebuilt model is cached again

    def test_a_new_commit_rebuilds(self, node, loop_cfg, checkout, sims_argv):
        node.build(loop_cfg)
        checkout["head"] = "decade"
        assert node.build(loop_cfg).reused is False
        assert len(sims_argv) == 2

    def test_an_edit_to_an_untracked_file_rebuilds(self, node, loop_cfg, checkout, sims_argv, stub_piton_root):
        added = stub_piton_root / "piton" / "unit_top.cpp"
        added.write_text("int main() { return 0; }\n")
        checkout["untracked"] = "piton/unit_top.cpp\0"
        node.build(loop_cfg)
        added.write_text("int main() { return 1; }\n")
        assert node.build(loop_cfg).reused is False
        assert len(sims_argv) == 2

    def test_a_different_verilator_rebuilds(self, node, checkout, sims_argv, monkeypatch):
        cfg = PitonConfig(core="sparc")  # no recorded version, as in the loop
        version = {"text": "Verilator 5.020 2024-01-01"}
        monkeypatch.setattr(
            OpenPitonWorkspaceNode,
            "verilator_version_text",
            staticmethod(lambda root, core="ariane", timeout_seconds=120: version["text"]),
        )
        node.build(cfg)
        version["text"] = "Verilator 5.052 2025-11-01"
        assert node.build(cfg).reused is False
        assert len(sims_argv) == 2

    def test_a_marker_without_a_fingerprint_rebuilds_once(self, node, loop_cfg, checkout, sims_argv):
        """Markers written before the fingerprint existed hold the key alone."""
        first = node.build(loop_cfg)
        with open(os.path.join(first.model_dir, ".mace_build_ok"), "w") as f:
            f.write(loop_cfg.key)
        assert node.build(loop_cfg).reused is False
        assert node.build(loop_cfg).reused is True
        assert len(sims_argv) == 2

    def test_a_stale_model_is_removed_before_the_rebuild(self, node, loop_cfg, checkout, monkeypatch):
        """If the rebuild fails, the old binary must not look like its output."""
        node.build(loop_cfg)
        checkout["diff"] = "+ an RTL edit that breaks the build"
        monkeypatch.setenv("FAKE_SIMS_FAIL_BUILD", "1")
        failed = node.build(loop_cfg)
        assert failed.success is False
        assert failed.binary_path == ""
        assert not os.path.exists(os.path.join(failed.model_dir, "obj_dir"))


class TestSourceFingerprintOnAGitCheckout:
    """source_fingerprint against a git repository, with no stubbed git."""

    @pytest.fixture
    def repo(self, stub_piton_root):
        import shutil
        import subprocess

        if shutil.which("git") is None:
            pytest.skip("git is not installed")

        def git(*args):
            subprocess.run(["git", *args], cwd=stub_piton_root, check=True, capture_output=True)

        (stub_piton_root / ".gitignore").write_text("build/\n*.tmp.v\n")
        git("init", "-q")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "stub")
        return stub_piton_root

    def fingerprint(self, root):
        return OpenPitonWorkspaceNode.source_fingerprint(str(root), "Verilator 5.020 2024-01-01")

    def test_ignored_build_outputs_leave_it_unchanged(self, repo):
        before = self.fingerprint(repo)
        (repo / "piton" / "pc_cmp.tmp.v").write_text("// generated\n")
        (repo / "build" / "model.log").write_text("built\n")
        assert self.fingerprint(repo) == before

    def test_a_tracked_edit_changes_it(self, repo):
        before = self.fingerprint(repo)
        settings = repo / "piton" / "piton_settings.bash"
        settings.write_text(settings.read_text() + "# edited\n")
        assert self.fingerprint(repo) != before

    def test_a_new_untracked_file_changes_it(self, repo):
        before = self.fingerprint(repo)
        (repo / "piton" / "new_top.v").write_text("module new_top; endmodule\n")
        assert self.fingerprint(repo) != before

    def test_the_verilator_version_changes_it(self, repo):
        other = OpenPitonWorkspaceNode.source_fingerprint(str(repo), "Verilator 5.052 2025-11-01")
        assert other != self.fingerprint(repo)

    def test_a_build_does_not_change_its_own_checkout(self, repo, sims_argv):
        """The stub sims writes a .tmp.v into the source tree, as pyHP does."""
        node = OpenPitonWorkspaceNode(str(repo), require_colocated=False)
        cfg = PitonConfig(core="sparc", verilator_version="Verilator 4.014 2019-01-01")
        node.build(cfg)
        assert node.build(cfg).reused is True
        assert len(sims_argv) == 1


class TestBuildDetectsStaleAddressMap:
    """configure(address_map=...) writes straight into this checkout's one
    shared piton/verif/env/manycore -- not scoped by build_id. build() must
    refuse rather than silently compile (and permanently cache) against a
    different config's map if the checkout changed since this config was
    created -- see configure()'s and build()'s own docstrings. A config
    constructed directly recorded no checkout state, so it is never refused.
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

    def test_a_config_with_no_recorded_diff_still_proceeds_when_checkout_is_still_clean(
        self, node, cfg, monkeypatch
    ):
        """The overwhelmingly common case (no address_map ever used): the
        recheck still runs (see the next test for why it must), but a
        still-clean checkout matches this config's own empty diff, so
        build() proceeds normally."""
        monkeypatch.setattr(OpenPitonWorkspaceNode, "_git", lambda root, args, timeout_seconds=60: "")
        assert cfg.diff == ""
        art = node.build(cfg)
        assert art.success is True

    def test_a_config_with_no_recorded_diff_is_still_protected_from_a_later_dirty_configure(
        self, node, cfg, monkeypatch
    ):
        """A config built from a clean checkout (diff="") must be refused
        just as readily as a dirty one if a LATER configure(address_map=...)
        call on the same checkout has since made it dirty -- skipping this
        recheck just because the original diff was empty was the actual bug:
        that case got zero protection while its mirror image (dirty config,
        then a second dirty configure()) was already caught."""
        monkeypatch.setattr(
            OpenPitonWorkspaceNode,
            "_git",
            lambda root, args, timeout_seconds=60: "+ a map added by a later configure() call",
        )
        assert cfg.diff == ""
        with pytest.raises(ValueError, match="no longer matches"):
            node.build(cfg)

    def test_a_directly_constructed_config_builds_a_checkout_with_uncommitted_edits(
        self, node, monkeypatch
    ):
        """A config built with PitonConfig(...) instead of configure() -- as
        mace's loop builds every task's -- records no checkout state
        (source_rev and diff both empty), so there is nothing to recheck.
        Refusing it compared the checkout to an empty diff and failed every
        build on a freshly patched checkout, whose fixes 7 and 10 leave
        uncommitted edits under piton/verif/env/manycore."""
        cfg = PitonConfig(core="sparc", verilator_version="Verilator 4.014 2019-01-01")
        monkeypatch.setattr(
            OpenPitonWorkspaceNode,
            "_git",
            lambda root, args, timeout_seconds=60: "+ patch_openpiton.sh fix 10",
        )
        assert cfg.source_rev == "" and cfg.diff == ""
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

    def test_run_dirs_still_differ_when_two_runs_land_in_the_same_millisecond(
        self, node, cfg, monkeypatch
    ):
        """Regression test: run_dir's suffix used to be
        int(time.time() * 1000) % 100000, unique only for 100s -- two runs
        of the same test within that window collided on one directory, and
        makedirs(exist_ok=True) never caught it. Freeze time.time() so both
        calls land in the identical millisecond, which the old scheme could
        not tell apart at all."""
        import chia_openpiton.openpiton_workspace as ws

        monkeypatch.setattr(ws.time, "time", lambda: 1700000000.0)
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

    def test_max_bytes_per_file_zero_is_a_real_cap_not_no_cap(self, node, stub_piton_root):
        """0 is falsy in Python, so `if max_bytes_per_file and ...` used to
        treat max_bytes_per_file=0 the same as None (no cap) -- a caller
        asking for a strict list-only mode got full file contents instead.
        """
        (stub_piton_root / "build" / "small.log").write_text("ok")
        got = node.collect(str(stub_piton_root / "build"), ("*.log",), max_bytes_per_file=0)
        assert got.files == {}
        assert got.skipped["small.log"] == 2
        assert got.listing["small.log"] == 2

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
