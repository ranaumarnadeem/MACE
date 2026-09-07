"""chia_openpiton.openpiton_workspace — OpenPiton build/run primitives as CHIA nodes.

Modelled on :class:`chia.esp.esp_workspace.EspWorkspaceNode`. One instance = one
OpenPiton checkout on one worker, with every member pinned to a single
placement-group bundle: ``sims`` keeps its state on disk (the model directory
under ``$PITON_ROOT/build/manycore/<build_id>/``, the run directory beside it),
so configure -> build -> run must land on the same machine.

**One checkout per concurrent build, not one per worker.** OpenPiton's template
preprocessor (pyHP) writes generated ``.tmp.v`` files back into the *source
tree* on every build, so two builds with different tile counts in one checkout
race and corrupt each other. A worker advertising ``{"openpiton": 2}`` must
therefore host two separate checkouts, each with its own node instance.

Members are ``@staticmethod``s taking ``piton_root`` first, matching upstream's
convention so this package can move into ``chia/openpiton/`` unchanged.
Constructing a node binds that root, so instance calls omit it:

    node = OpenPitonWorkspaceNode("/work/openpiton")
    art = get(node.build.chia_remote(cfg))          # root is bound
    art = get(OpenPitonWorkspaceNode.build.chia_remote(root, cfg))   # unpinned
"""

from __future__ import annotations

import glob as _glob
import logging
import os
import shlex
import signal
import subprocess
import time

from chia.base.ChiaFunction import ChiaFunction
from chia.base.colocated import ColocatedNode

from chia_openpiton import parse
from chia_openpiton.state_def import (
    DEFAULT_CACHES,
    SIM_TYPES,
    PitonBuildArtifact,
    PitonCollectResult,
    PitonConfig,
    PitonRegressResult,
    PitonRunResult,
)

logger = logging.getLogger(__name__)

# How much of a log we ship back by value. Full logs stay on the worker and are
# fetched deliberately with collect(); a chatty Verilator build is megabytes and
# would inflate the object store on every call.
LOG_TAIL_BYTES = 8000

# OpenPiton's Verilator model, relative to a model directory.
MODEL_BINARY = "obj_dir/Vcmp_top"

# Written after a successful build, alongside the binary. Presence of the
# binary alone isn't proof of a good build: a worker killed mid-link can leave
# a truncated file. The marker is only written after `build()` has already
# confirmed success, so its presence is the actual signal to trust.
BUILD_OK_MARKER = ".mace_build_ok"


def _require_root(piton_root: object) -> str:
    """Validate ``piton_root`` before anything touches the filesystem.

    This guard exists because of a real bug: the members are staticmethods with
    ``piton_root`` first, so an instance-style call that also passed the node
    object sent a node where a path belonged. ``os.makedirs`` ran before the
    failure surfaced and created a directory literally named
    ``<...OpenPitonWorkspaceNode object at 0x...>``.
    """
    if not isinstance(piton_root, str):
        raise ValueError(
            f"piton_root must be a path string, got {type(piton_root).__name__}. "
            "Call node.<member>.chia_remote(...) without a root (the instance "
            "binds it), or Class.<member>.chia_remote(root, ...) with one."
        )
    if not piton_root.strip():
        raise ValueError("piton_root must not be empty")
    if not os.path.isdir(piton_root):
        raise ValueError(f"piton_root is not a directory: {piton_root!r}")
    return os.path.abspath(piton_root)


def _env_prefix(piton_root: str, core: str) -> str:
    """Shell prologue reproducing OpenPiton's documented environment.

    Mirrors ``piton/ariane_setup.sh`` and the ``before_script`` block of
    OpenPiton's own ``.gitlab-ci.yml``. Two details are load-bearing:
    ``piton_settings.bash`` does *not* set ``PITON_ROOT`` (it expects it to be
    exported already), and ``ARIANE_ROOT`` must carry a trailing slash.
    """
    lines = [f"export PITON_ROOT={shlex.quote(piton_root)}"]
    if core == "ariane":
        lines += [
            'export ARIANE_ROOT="$PITON_ROOT/piton/design/chip/tile/ariane/"',
            'export RISCV="${RISCV:-$HOME/scratch/riscv_install}"',
            # Only export VERILATOR_ROOT when it points at a real install.
            # ariane_setup.sh sets it unconditionally to a path inside the
            # submodule, but Verilator locates its own data files through this
            # variable -- pointing it at a missing or half-built tree breaks a
            # perfectly good system Verilator. Workers that build the pinned
            # 4.014 get it; workers using a packaged Verilator keep their own.
            'if [ -z "${VERILATOR_ROOT:-}" ] && '
            '[ -x "$ARIANE_ROOT/tmp/verilator-4.014/bin/verilator" ]; then '
            'export VERILATOR_ROOT="$ARIANE_ROOT/tmp/verilator-4.014/"; fi',
            'export LIBRARY_PATH="$RISCV/lib"',
            'export LD_LIBRARY_PATH="$RISCV/lib:$LD_LIBRARY_PATH"',
        ]
    lines.append('source "$PITON_ROOT/piton/piton_settings.bash"')
    if core == "ariane":
        # AFTER sourcing, deliberately. piton_settings.bash prepends
        # "$DV_ROOT/tools/bin:$CC_BIN" (CC_BIN is /usr/bin) to PATH, so a
        # distro riscv64-unknown-elf-gcc shadows ours -- and Ubuntu's package
        # ships no newlib, so every diag fails with "string.h: No such file".
        # Re-prepending here keeps the toolchain we actually installed.
        lines += [
            'export PATH="$RISCV/bin:$PATH"',
            'if [ -n "${VERILATOR_ROOT:-}" ]; then '
            'export PATH="$VERILATOR_ROOT/bin:$PATH"; fi',
        ]
    return " && ".join(lines) + " && "


def _run(
    command: str,
    piton_root: str,
    core: str,
    cwd: str,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> tuple[str, str, int, float]:
    """Run one OpenPiton command; never raises.

    Returns ``(stdout, stderr, returncode, wall_seconds)`` with
    ``returncode == -1`` on timeout, partial output preserved and a marker
    appended to stderr. ``start_new_session`` puts the whole tool tree in one
    process group so a timeout can kill every descendant -- without it a killed
    shell leaves grandchildren (verilator, g++) holding the pipes open and the
    call stalls in cleanup.
    """
    full = _env_prefix(piton_root, core) + f"cd {shlex.quote(cwd)} && {command}"
    merged = {**os.environ, **(env or {})}
    started = time.time()
    logger.info("Running: %s (cwd=%s)", command, cwd)
    try:
        proc = subprocess.Popen(
            ["bash", "-lc", full],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=merged,
        )
    except OSError as e:
        logger.error("could not launch: %s", e)
        return "", str(e), -1, time.time() - started

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        stdout, stderr = proc.communicate()
        stderr = (stderr or "") + f"\nsims timed out after {timeout_seconds}s"
        rc = -1
    wall = time.time() - started
    if rc != 0:
        logger.error("command failed (rc=%s); stderr tail: %s", rc, (stderr or "")[-500:])
    return stdout or "", stderr or "", rc, wall


def _tail(text: str, limit: int = LOG_TAIL_BYTES) -> str:
    return text[-limit:] if text else ""


def _read_if_present(path: str, limit: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read()[-limit:]
    except OSError:
        return ""


def _resolve_under(base_dir: str, relpath: str) -> str:
    """Absolute path of *relpath* under *base_dir*; ValueError on escape."""
    base_dir = os.path.abspath(base_dir)
    path = os.path.normpath(os.path.join(base_dir, relpath))
    if path != base_dir and not path.startswith(base_dir + os.sep):
        raise ValueError(f"relpath {relpath!r} escapes base dir {base_dir!r}")
    return path


class _RootBoundChiaFn:
    """A placement-pinned ``@ChiaFunction`` member with ``piton_root`` bound.

    Deliberately not a closure over the root: a plain object with explicit
    attributes serialises predictably and keeps ``options(...)`` chainable.
    Class-level introspection (``ColocatedNode._member_demands``) reads the
    *class* attribute, so binding at the instance level does not disturb it.
    """

    __slots__ = ("_fn", "_opts", "_root")

    def __init__(self, fn, scheduling_opts: dict | None, piton_root: str):
        self._fn = fn
        self._opts = dict(scheduling_opts or {})
        self._root = piton_root

    def _handle(self):
        return self._fn.options(**self._opts) if self._opts else self._fn

    def chia_remote(self, *args, **kwargs):
        return self._handle().chia_remote(self._root, *args, **kwargs)

    def chia_remote_blocking(self, *args, **kwargs):
        return self._handle().chia_remote_blocking(self._root, *args, **kwargs)

    def options(self, **overrides):
        return _RootBoundChiaFn(self._fn, {**self._opts, **overrides}, self._root)

    def __call__(self, *args, **kwargs):
        """Local (non-Ray) invocation, with the root prepended."""
        return self._fn(self._root, *args, **kwargs)


class OpenPitonWorkspaceNode(ColocatedNode):
    """One OpenPiton checkout: configure / build / run / collect, one placement.

    Usage::

        with OpenPitonWorkspaceNode("/work/openpiton") as node:
            cfg = get(node.configure.chia_remote(x_tiles=1, y_tiles=1))
            art = get(node.build.chia_remote(cfg))
            res = get(node.run.chia_remote(cfg, "hello_world.c"))
            assert res.success
    """

    _MEMBER_FNS = (
        "sims",
        "configure",
        "build",
        "run",
        "regress",
        "put_file",
        "collect",
        "clean",
    )
    _DEFAULT_BUNDLE = {"CPU": 1, "openpiton": 1}

    def __init__(self, piton_root: str, **kwargs):
        self.piton_root = _require_root(piton_root)
        super().__init__(**kwargs)
        # Rebind each member with the root prepended, replacing the plain
        # PinnedChiaFn wrappers ColocatedNode.__init__ just installed.
        for name in self._MEMBER_FNS:
            setattr(
                self,
                name,
                _RootBoundChiaFn(getattr(type(self), name), self._sched_opts, self.piton_root),
            )

    # -- generic escape hatch --------------------------------------------------

    @staticmethod
    @ChiaFunction(resources={"openpiton": 1})
    def sims(
        piton_root: str,
        args: str,
        core: str = "ariane",
        cwd: str | None = None,
        timeout_seconds: int = 3600,
    ) -> tuple[int, str, str]:
        """Run ``sims <args>`` verbatim. The escape hatch behind the typed members.

        Args:
            piton_root: OpenPiton checkout root on the worker.
            args: Everything after ``sims``, as a shell string.
            core: Selects the environment prologue ("ariane" adds ARIANE_ROOT,
                RISCV and VERILATOR_ROOT; "sparc" needs none of them).
            cwd: Directory to run in; defaults to ``$PITON_ROOT/build``.
            timeout_seconds: Wall-clock limit; ``returncode=-1`` on expiry.

        Returns:
            ``(returncode, stdout, stderr)``.
        """
        root = _require_root(piton_root)
        run_dir = cwd or os.path.join(root, "build")
        os.makedirs(run_dir, exist_ok=True)
        stdout, stderr, rc, _ = _run(f"sims {args}", root, core, run_dir, timeout_seconds)
        return rc, stdout, stderr

    # -- configuration ---------------------------------------------------------

    @staticmethod
    @ChiaFunction(resources={"openpiton": 1})
    def configure(
        piton_root: str,
        x_tiles: int = 1,
        y_tiles: int = 1,
        core: str = "ariane",
        network_config: str = "2dmesh_config",
        config_rtl: tuple[str, ...] = ("MINIMAL_MONITORING",),
        caches: dict[str, tuple[int, int]] | None = None,
        address_map: str | None = None,
        extra_flags: tuple[str, ...] = (),
        timeout_seconds: int = 300,
    ) -> PitonConfig:
        """Resolve a configuration against this checkout and return its identity.

        Validates the mesh and core, optionally writes file-level edits into the
        checkout (currently the device address map, which drives both the io_xbar
        decode ranges and the generated device tree), then records the source
        revisions and Verilator version so the returned config's ``key`` --
        and therefore its model directory -- reflects everything that changes
        the produced model.

        Args:
            piton_root: OpenPiton checkout root on the worker.
            x_tiles, y_tiles: Mesh dimensions; sims caps each axis at 256.
            core: ``"ariane"`` (RISC-V) or ``"sparc"``.
            network_config: ``"2dmesh_config"`` or ``"xbar_config"``. Always
                passed explicitly -- leaving it unset makes sims default to the
                string ``"2d_mesh"``, which pyhplib does not recognise.
            config_rtl: ``define`` names written into the model's ``config.v``.
            caches: ``{"l2": (size_bytes, associativity), ...}``; defaults from
                OpenPiton's ``manycore.config``.
            address_map: Full replacement text for the simulation device map
                (``piton/verif/env/manycore/devices_ariane.xml``). None leaves
                the checkout untouched.
            extra_flags: Any further sims flags, appended verbatim.
            timeout_seconds: Limit for the revision/version probes.

        Returns:
            A :class:`PitonConfig`; its ``diff`` captures any file edits made.

        Raises:
            ValueError: On an invalid mesh, core, network config or cache name.
        """
        root = _require_root(piton_root)

        if address_map is not None:
            target = _resolve_under(
                root, os.path.join("piton", "verif", "env", "manycore", "devices_ariane.xml")
            )
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as f:
                f.write(address_map)
            logger.info("wrote %d chars to %s", len(address_map), target)

        source_rev = OpenPitonWorkspaceNode._git(root, ["rev-parse", "HEAD"], timeout_seconds)
        ariane_rev = OpenPitonWorkspaceNode._git(
            root,
            ["rev-parse", "HEAD:piton/design/chip/tile/ariane"],
            timeout_seconds,
        )
        diff = OpenPitonWorkspaceNode._git(
            root, ["diff", "--", "piton/verif/env/manycore"], timeout_seconds
        )
        version = OpenPitonWorkspaceNode.verilator_version_text(root, core, timeout_seconds)

        return PitonConfig(
            core=core,
            x_tiles=x_tiles,
            y_tiles=y_tiles,
            network_config=network_config,
            config_rtl=tuple(config_rtl),
            caches=dict(caches) if caches else dict(DEFAULT_CACHES),
            extra_flags=tuple(extra_flags),
            source_rev=source_rev,
            ariane_rev=ariane_rev,
            verilator_version=version.strip(),
            diff=diff,
        )

    @staticmethod
    def _git(root: str, args: list[str], timeout_seconds: int = 60) -> str:
        """Best-effort ``git`` query inside the checkout; "" when unavailable."""
        try:
            done = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout_seconds
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""

    @staticmethod
    def verilator_version_text(root: str, core: str = "ariane", timeout_seconds: int = 120) -> str:
        """``verilator --version`` as seen *inside OpenPiton's environment*.

        Deliberately not a bare ``verilator --version`` on the host PATH:
        ``ariane_setup.sh`` prepends ``$VERILATOR_ROOT/bin``, so the Verilator a
        build actually uses can differ from the one a plain shell finds. The
        ``--no-timing`` decision depends on this, and getting it from the wrong
        binary produces a build that fails on a flag mismatch.
        """
        stdout, stderr, rc, _ = _run(
            "verilator --version", root, core, root, timeout_seconds
        )
        return stdout if rc == 0 else (stdout or stderr)

    # -- build -----------------------------------------------------------------

    @staticmethod
    @ChiaFunction(resources={"openpiton": 1})
    def build(
        piton_root: str,
        config: PitonConfig,
        sim_type: str = "vlt",
        clean: bool = False,
        extra_build_args: tuple[str, ...] = (),
        timeout_seconds: int = 7200,
    ) -> PitonBuildArtifact:
        """Build a simulation model: ``sims ... -<sim>_build -build_id=<key>``.

        Every configuration gets its own ``-build_id`` (from
        :attr:`PitonConfig.build_id`), because sims otherwise writes every model
        to ``rel-0.1`` and two configurations silently overwrite each other. That
        same key means a config identical in everything that affects the model
        (mesh, core, RTL defines, caches, extra flags, source revisions,
        Verilator version) reliably produces the same binary -- so unless
        ``clean=True``, a build whose marker (written only after a prior success)
        already exists is served from disk instead of re-invoking ``sims``. This
        is what makes "agent iterations that only change the test being run
        never rebuild" true without needing CHIA's cache machinery.

        ``--no-timing`` is added for Verilator 5 only, decided from the version
        reported inside OpenPiton's own environment: v5 refuses OpenPiton's bare
        ``#1`` delays without an explicit timing choice, and v4 has no such flag
        and errors if given one.

        Args:
            piton_root: OpenPiton checkout root on the worker.
            config: The configuration to build, from :meth:`configure`.
            sim_type: Simulator selector; ``"vlt"`` (Verilator) is the only
                license-free option.
            clean: Force a rebuild even if this build_id already succeeded.
                Also removes this build_id's ``obj_dir`` first -- sims' own
                ``-clean`` only removes VCS leftovers and never touches
                Verilator output, so this is done here.
            extra_build_args: Extra ``-<sim>_build_args=`` values.
            timeout_seconds: Wall-clock limit; ``returncode=-1`` on expiry.

        Returns:
            A :class:`PitonBuildArtifact`; ``success`` requires exit 0 *and* the
            model binary to exist. ``reused=True`` when a prior build was
            served from disk instead of invoking ``sims`` again.

        Raises:
            ValueError: On an unknown ``sim_type`` or an invalid root.
        """
        root = _require_root(piton_root)
        if sim_type not in SIM_TYPES:
            raise ValueError(f"sim_type must be one of {sorted(SIM_TYPES)}, got {sim_type!r}")

        model_dir = os.path.join(root, "build", "manycore", config.build_id)
        binary = os.path.join(model_dir, MODEL_BINARY)
        marker = os.path.join(model_dir, BUILD_OK_MARKER)

        if not clean and os.path.exists(marker) and os.path.exists(binary):
            logger.info("reusing prior build at %s (build_id=%s)", model_dir, config.build_id)
            return PitonBuildArtifact(
                success=True,
                returncode=0,
                config=config,
                sim_type=sim_type,
                model_dir=model_dir,
                binary_path=binary,
                wall_time_s=0.0,
                verilator_version=config.verilator_version,
                cache_key=config.key,
                reused=True,
            )

        if clean:
            obj_dir = os.path.join(model_dir, "obj_dir")
            if os.path.isdir(obj_dir):
                import shutil

                shutil.rmtree(obj_dir, ignore_errors=True)
                logger.info("removed %s", obj_dir)
            try:
                os.remove(marker)
            except FileNotFoundError:
                pass

        build_args = list(extra_build_args)
        version_text = config.verilator_version or OpenPitonWorkspaceNode.verilator_version_text(
            root, config.core
        )
        if sim_type == "vlt" and parse.needs_no_timing(version_text):
            if not any("timing" in a for a in build_args):
                build_args.append("--no-timing")

        argv = [*config.sims_flags(), f"-build_id={config.build_id}", f"-{sim_type}_build"]
        argv += [f"-{sim_type}_build_args={a}" for a in build_args]

        run_dir = os.path.join(root, "build")
        os.makedirs(run_dir, exist_ok=True)
        stdout, stderr, rc, wall = _run(
            "sims " + " ".join(shlex.quote(a) for a in argv),
            root,
            config.core,
            run_dir,
            timeout_seconds,
        )

        built = os.path.exists(binary)
        success = rc == 0 and built
        reason = "" if success else (parse.build_failure_reason(stdout, stderr) or "no_model_binary")
        if rc == 0 and not built:
            logger.error("sims exited 0 but %s was not produced", binary)
        if success:
            # Written last, and only on a confirmed-good build: its presence
            # is what a later call trusts to skip rebuilding, so it must never
            # exist next to a truncated or failed binary.
            with open(marker, "w") as f:
                f.write(config.key)

        return PitonBuildArtifact(
            success=success,
            returncode=rc,
            config=config,
            sim_type=sim_type,
            model_dir=model_dir,
            binary_path=binary if built else "",
            wall_time_s=wall,
            verilator_version=version_text.strip(),
            cache_key=config.key,
            failure_reason=reason,
            stdout=_tail(stdout),
            stderr=_tail(stderr),
        )

    # -- run -------------------------------------------------------------------

    @staticmethod
    @ChiaFunction(resources={"openpiton": 1})
    def run(
        piton_root: str,
        config: PitonConfig,
        test: str,
        sim_type: str = "vlt",
        precompiled: bool = False,
        asm_diag_root: str | None = None,
        finish_mask: str | None = None,
        rtl_timeout: int | None = None,
        max_cycle: int | None = None,
        extra_run_args: tuple[str, ...] = (),
        timeout_seconds: int = 3600,
    ) -> PitonRunResult:
        """Run one diag against a built model and judge it from the transcript.

        Success is decided by the testbench monitor's verdict line, never by the
        exit code: an RTL simulation exits 0 whether or not the program passed.

        Each run gets a fresh directory under ``<model>/runs/`` because sims
        writes its artifacts (``sim.log``, ``status.log``, ``mem.image``,
        ``symbol.tbl``, ``fake_uart.log``) into the current directory.

        Args:
            piton_root: OpenPiton checkout root on the worker.
            config: The configuration whose model to run against.
            test: Diag name, e.g. ``"hello_world.c"`` or ``"rv64ui-p-addi.S"``.
            sim_type: Simulator selector, matching the build.
            precompiled: Use a prebuilt riscv-tests ELF instead of compiling the
                source (sims then searches ``$ARIANE_ROOT/tmp/riscv-tests/build``).
            asm_diag_root: Extra directory to search for the diag source; this
                is how our own gate workloads are found.
            finish_mask: ``+finish_mask`` value; defaults to one digit per tile,
                so a multi-tile run only passes when every hart traps good.
            rtl_timeout: ``+TIMEOUT`` in cycles.
            max_cycle: ``+max_cycle`` abort threshold.
            extra_run_args: Further sims flags, appended verbatim.
            timeout_seconds: Wall-clock limit; ``returncode=-1`` on expiry.

        Returns:
            A :class:`PitonRunResult` with a tri-state ``verdict``.
        """
        root = _require_root(piton_root)
        if sim_type not in SIM_TYPES:
            raise ValueError(f"sim_type must be one of {sorted(SIM_TYPES)}, got {sim_type!r}")

        model_dir = os.path.join(root, "build", "manycore", config.build_id)
        safe_test = test.replace("/", "_")
        run_dir = os.path.join(model_dir, "runs", f"{safe_test}-{int(time.time() * 1000) % 100000}")
        os.makedirs(run_dir, exist_ok=True)

        argv = [*config.sims_flags(), f"-build_id={config.build_id}"]
        if precompiled:
            argv.append("-precompiled")
        if asm_diag_root:
            argv.append(f"-asm_diag_root={asm_diag_root}")
        mask = finish_mask if finish_mask is not None else config.finish_mask
        if mask:
            argv.append(f"-finish_mask={mask}")
        if rtl_timeout is not None:
            argv.append(f"-rtl_timeout={rtl_timeout}")
        if max_cycle is not None:
            argv.append(f"-max_cycle={max_cycle}")
        argv.extend(extra_run_args)
        argv += [f"-{sim_type}_run", test]

        stdout, stderr, rc, wall = _run(
            "sims " + " ".join(shlex.quote(a) for a in argv),
            root,
            config.core,
            run_dir,
            timeout_seconds,
        )

        sim_log = _read_if_present(os.path.join(run_dir, "sim.log")) or stdout
        status_log = _read_if_present(os.path.join(run_dir, "status.log"))
        verdict = parse.sim_verdict(sim_log)
        if verdict is None and rc == -1:
            verdict = "timeout"

        return PitonRunResult(
            success=PitonRunResult.decide(rc, verdict),
            returncode=rc,
            test=test,
            sim_type=sim_type,
            run_dir=run_dir,
            verdict=verdict,
            sim_time=parse.sim_time(sim_log),
            cycles=parse.cycles(status_log),
            exec_cycles=parse.exec_cycles(status_log),
            wall_time_s=wall,
            sim_log_tail=_tail(sim_log),
            status_log=status_log,
            fake_uart=_read_if_present(os.path.join(run_dir, "fake_uart.log")),
            stdout=_tail(stdout),
            stderr=_tail(stderr),
        )

    @staticmethod
    @ChiaFunction(resources={"openpiton": 1})
    def regress(
        piton_root: str,
        config: PitonConfig,
        tests: tuple[str, ...],
        group: str = "",
        sim_type: str = "vlt",
        precompiled: bool = False,
        asm_diag_root: str | None = None,
        timeout_seconds: int = 3600,
    ) -> PitonRegressResult:
        """Run several diags serially against one model.

        This is the single-worker fallback. Prefer fanning :meth:`run` out
        across workers from the loop, which is where cancellation and failure
        budgets belong -- a serial regression on one worker is only useful for
        a smoke check.
        """
        root = _require_root(piton_root)
        results = [
            OpenPitonWorkspaceNode.run(
                root,
                config,
                test,
                sim_type=sim_type,
                precompiled=precompiled,
                asm_diag_root=asm_diag_root,
                timeout_seconds=timeout_seconds,
            )
            for test in tests
        ]
        failures = [r for r in results if not r.success]
        return PitonRegressResult(
            success=bool(results) and not failures,
            group=group,
            sim_type=sim_type,
            num_tests=len(results),
            num_failures=len(failures),
            results=results,
            results_dir=os.path.join(root, "build", "manycore", config.build_id, "runs"),
        )

    # -- workspace files -------------------------------------------------------

    @staticmethod
    @ChiaFunction(resources={"openpiton": 1})
    def put_file(piton_root: str, relpath: str, content: bytes | str) -> str:
        """Write *content* to ``<piton_root>/<relpath>``; returns the path written.

        ``relpath`` may not escape the checkout (``ValueError``).
        """
        root = _require_root(piton_root)
        path = _resolve_under(root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        with open(path, "wb") as f:
            f.write(data)
        logger.info("wrote %d bytes to %s", len(data), path)
        return path

    @staticmethod
    @ChiaFunction(resources={"openpiton": 1})
    def collect(
        piton_root: str,
        base_dir: str,
        patterns: tuple[str, ...],
        max_bytes_per_file: int | None = None,
    ) -> PitonCollectResult:
        """Fetch text files from a previous run's directory on this worker.

        Args:
            piton_root: OpenPiton checkout root on the worker.
            base_dir: Directory to glob under (absolute, or relative to the root).
            patterns: Globs relative to *base_dir*; ``**`` is recursive.
            max_bytes_per_file: Files over this size are recorded in ``skipped``
                rather than shipped -- protects against a glob matching a model
                binary or a multi-megabyte waveform.
        """
        root = _require_root(piton_root)
        base = base_dir if os.path.isabs(base_dir) else _resolve_under(root, base_dir)
        files: dict[str, str] = {}
        skipped: dict[str, int] = {}
        listing: dict[str, int] = {}

        for pattern in patterns:
            for path in _glob.glob(os.path.join(base, pattern), recursive=True):
                if not os.path.isfile(path):
                    continue
                rel = os.path.relpath(path, base)
                if rel in files or rel in skipped:
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                listing[rel] = size
                if max_bytes_per_file and size > max_bytes_per_file:
                    skipped[rel] = size
                    continue
                with open(path, errors="replace") as f:
                    files[rel] = f.read()
        if skipped:
            logger.warning("collect skipped %d file(s) over the cap", len(skipped))
        return PitonCollectResult(base_dir=base, files=files, skipped=skipped, listing=listing)

    @staticmethod
    @ChiaFunction(resources={"openpiton": 1})
    def clean(piton_root: str, config: PitonConfig) -> bool:
        """Remove this configuration's model directory. Returns whether it existed.

        sims' own ``-clean`` removes only VCS leftovers (``csrc``, ``simv``,
        ``simv.daidir``, ``AxisWork``) and never touches ``obj_dir``, so
        Verilator models are cleaned here instead.
        """
        import shutil

        root = _require_root(piton_root)
        model_dir = os.path.join(root, "build", "manycore", config.build_id)
        if not os.path.isdir(model_dir):
            return False
        shutil.rmtree(model_dir, ignore_errors=True)
        logger.info("removed %s", model_dir)
        return True
