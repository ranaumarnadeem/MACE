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

# OpenPiton's Verilator model for the manycore sys, relative to a model
# directory. A non-manycore sys (a unit-test environment) builds a
# differently-named binary matching its own -toplevel= (e.g. ifu_esl_lfsr's
# is Vifu_esl_lfsr_top, not Vcmp_top) -- see _find_model_binary(), which
# globs for it rather than assuming a fixed name for every sys.
MODEL_BINARY = "obj_dir/Vcmp_top"

# See OpenPitonWorkspaceNode.verilator_version_text's own docstring.
_VERILATOR_VERSION_CACHE: dict[tuple[str, str], str] = {}


def _find_model_binary(model_dir: str, sys: str) -> str:
    """The built Verilator binary under *model_dir*, or ``""`` if absent.

    manycore's own binary name (Vcmp_top) is known and checked directly, both
    because it's the overwhelmingly common case and so a glob never has to
    disambiguate between a real binary and Verilator's other obj_dir output
    (.d/.o files, a matching-prefix intermediate). Any other sys's toplevel
    name isn't something this adapter curates per environment, so it globs
    obj_dir for the one executable V<toplevel> file sims' own -vlt_build
    produces there.
    """
    if sys == "manycore":
        candidate = os.path.join(model_dir, MODEL_BINARY)
        return candidate if os.path.exists(candidate) else ""
    obj_dir = os.path.join(model_dir, "obj_dir")
    matches = [
        p
        for p in _glob.glob(os.path.join(obj_dir, "V*"))
        if os.path.isfile(p) and os.access(p, os.X_OK) and "." not in os.path.basename(p)
    ]
    return matches[0] if len(matches) == 1 else ""

# Written after a successful build, alongside the binary. Presence of the
# binary alone isn't proof of a good build: a worker killed mid-link can leave
# a truncated file. The marker is only written after `build()` has already
# confirmed success, so its presence is the actual signal to trust.
BUILD_OK_MARKER = ".mace_build_ok"


def _require_root(piton_root: object, check_exists: bool = True) -> str:
    """Validate ``piton_root`` before anything touches the filesystem.

    This guard exists because of a real bug: the members are staticmethods with
    ``piton_root`` first, so an instance-style call that also passed the node
    object sent a node where a path belonged. ``os.makedirs`` ran before the
    failure surfaced and created a directory literally named
    ``<...OpenPitonWorkspaceNode object at 0x...>``.

    ``check_exists=False`` (only set via ``OpenPitonWorkspaceNode``'s explicit
    ``root_on_remote_worker=True`` -- see its docstring; deliberately NOT
    inferred from ``require_colocated``, which this test suite also passes
    False just to skip placement-group reservation for fast local stub
    testing, where the root very much should still be checked) skips the
    local directory check and local ``abspath`` normalization: on a
    multi-machine cluster the checkout can legitimately live only on a
    remote worker, invisible to whatever process constructs the node. An
    absolute-path check is the only thing this process can honestly verify
    in that case -- a relative root would resolve against the wrong
    machine's cwd wherever it actually dispatches.
    """
    if not isinstance(piton_root, str):
        raise ValueError(
            f"piton_root must be a path string, got {type(piton_root).__name__}. "
            "Call node.<member>.chia_remote(...) without a root (the instance "
            "binds it), or Class.<member>.chia_remote(root, ...) with one."
        )
    if not piton_root.strip():
        raise ValueError("piton_root must not be empty")
    if not check_exists:
        if not os.path.isabs(piton_root):
            raise ValueError(
                f"piton_root must be an absolute path when root_on_remote_worker=True "
                f"(it may live only on a remote worker), got {piton_root!r}"
            )
        return piton_root
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
    """Run one OpenPiton command; never raises except ``KeyboardInterrupt``.

    Returns ``(stdout, stderr, returncode, wall_seconds)`` with
    ``returncode == -1`` on timeout, partial output preserved and a marker
    appended to stderr. ``start_new_session`` puts the whole tool tree in one
    process group so a timeout can kill every descendant -- without it a killed
    shell leaves grandchildren (verilator, g++) holding the pipes open and the
    call stalls in cleanup. The same process-group kill runs on a
    ``KeyboardInterrupt`` (Ctrl-C): ``start_new_session`` also means this
    subprocess tree never receives the terminal's own SIGINT, so without this
    it would keep running, orphaned, after Python itself has already moved
    on. That case is re-raised, not swallowed -- deciding what to tell the
    user belongs to the caller (see mace.cli.shell's own handling).
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
    except KeyboardInterrupt:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.communicate()
        raise
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


def _read_full_if_present(path: str) -> str:
    """Like :func:`_read_if_present`, but the whole file, untruncated -- for
    parsing (verdict/sim time/cycles), which must see the real end of a long
    transcript. A verbose manycore run where other tiles keep logging after
    the finishing tile's own PASS/FAIL line can push that line out of an
    arbitrary byte-count tail, misreporting a real pass as unclassified.
    Truncate separately, with :func:`_tail`, only what actually gets shipped
    back in the returned dataclass -- the same order :func:`build` already
    parses in (full text first, tail last).
    """
    try:
        with open(path, errors="replace") as f:
            return f.read()
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

    def __init__(self, piton_root: str, *, root_on_remote_worker: bool = False, **kwargs):
        """See ColocatedNode.__init__ for the placement-related kwargs.

        Args:
            piton_root: OpenPiton checkout root. Must be a real, local
                directory unless ``root_on_remote_worker`` says otherwise.
            root_on_remote_worker: set True only when *piton_root* is known
                to exist solely on a remote worker's filesystem (a
                multi-machine cluster where this constructor necessarily
                runs somewhere else) -- skips the local directory check
                (which would always, incorrectly, fail) in favor of an
                absolute-path check, and requires the caller to handle its
                own dispatch placement (pass ``require_colocated=False`` and
                pin scheduling itself; a self-reserved placement group would
                reserve capacity on the wrong machine just as easily as
                Ray's default scheduler would).
        """
        if root_on_remote_worker and kwargs.get("require_colocated", True):
            raise ValueError(
                "root_on_remote_worker=True requires require_colocated=False -- "
                "a self-reserved placement group cannot promise which machine "
                "it lands on, so it cannot promise the one holding piton_root."
            )
        self.piton_root = _require_root(piton_root, check_exists=not root_on_remote_worker)
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
        sys: str = "manycore",
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
                the checkout untouched. Written immediately, into this
                checkout's one shared copy of that file -- not scoped by the
                config this call is about to return. Call :meth:`build` for
                this config immediately after, before any other
                ``configure(address_map=...)`` call against the *same*
                checkout: :meth:`build` re-checks the checkout still matches
                what it computed and raises rather than silently building
                (and permanently caching) against a different config's map.
            extra_flags: Any further sims flags, appended verbatim.
            sys: ``-sys=`` value. ``"manycore"`` (default) is the full-chip
                mesh; any other name is a registered OpenPiton unit-test
                environment (``piton/tools/src/sims/<sys>.config``), in which
                case the mesh/core/cache args above are accepted but unused --
                see ``PitonConfig.sims_flags()``.
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
            sys=sys,
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

        Memoized per ``(root, core)``: this doesn't change within one
        process's lifetime, but :meth:`build` re-derives it on every
        non-cache-hit build whenever the config wasn't produced by
        :meth:`configure` (the only path that populates
        ``PitonConfig.verilator_version`` -- the real mace loop never calls
        it) -- not worth a fresh subprocess spawn (a full ``bash -lc``
        environment-sourcing shell) every time. Only a successful lookup is
        cached; a failure is retried on the next call, matching this
        function's own best-effort, never-raises posture.
        """
        key = (root, core)
        cached = _VERILATOR_VERSION_CACHE.get(key)
        if cached is not None:
            return cached
        stdout, stderr, rc, _ = _run(
            "verilator --version", root, core, root, timeout_seconds
        )
        text = stdout if rc == 0 else (stdout or stderr)
        if rc == 0:
            _VERILATOR_VERSION_CACHE[key] = text
        return text

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
            ValueError: On an unknown ``sim_type``, an invalid root, or (for a
                config with a recorded ``address_map`` diff) a checkout whose
                ``piton/verif/env/manycore`` no longer matches it -- see
                :meth:`configure`'s own docstring on why that can happen.
        """
        root = _require_root(piton_root)
        if sim_type not in SIM_TYPES:
            raise ValueError(f"sim_type must be one of {sorted(SIM_TYPES)}, got {sim_type!r}")

        if config.diff:
            # configure(address_map=...) writes straight into this checkout's
            # single shared piton/verif/env/manycore -- not scoped by
            # build_id. A second configure() call for a different config
            # against the same checkout overwrites it before this build ever
            # runs, so re-check now rather than silently compiling (and
            # permanently caching under *this* build_id) whatever the file
            # currently holds, which may no longer be what this config's own
            # diff -- and therefore its build_id -- was computed from.
            current_diff = OpenPitonWorkspaceNode._git(
                root, ["diff", "--", "piton/verif/env/manycore"], timeout_seconds
            )
            if current_diff != config.diff:
                raise ValueError(
                    f"checkout at {root!r} no longer matches build_id "
                    f"{config.build_id!r}'s recorded file edits -- another "
                    f"configure() call has changed piton/verif/env/manycore "
                    f"since this config was created. Call configure() again "
                    f"immediately before build() for this config, with no "
                    f"other configure() call against the same checkout in "
                    f"between."
                )

        model_dir = os.path.join(root, "build", config.sys, config.build_id)
        binary = _find_model_binary(model_dir, config.sys)
        marker = os.path.join(model_dir, BUILD_OK_MARKER)

        if not clean and os.path.exists(marker) and binary:
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

        binary = _find_model_binary(model_dir, config.sys)
        built = bool(binary)
        success = rc == 0 and built
        reason = "" if success else (parse.build_failure_reason(stdout, stderr) or "no_model_binary")
        if rc == 0 and not built:
            logger.error("sims exited 0 but no model binary was produced under %s", model_dir)
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

        model_dir = os.path.join(root, "build", config.sys, config.build_id)
        safe_test = test.replace("/", "_")
        run_dir = os.path.join(model_dir, "runs", f"{safe_test}-{int(time.time() * 1000) % 100000}")
        os.makedirs(run_dir, exist_ok=True)

        argv = [*config.sims_flags(), f"-build_id={config.build_id}"]
        if config.sys == "manycore":
            # precompiled/asm_diag_root/finish_mask/rtl_timeout/max_cycle and
            # the trailing "-<sim>_run <test>" diag-selection convention are
            # all manycore concepts (a diag to compile-or-find and simulate
            # against the full mesh). A unit-test sys's own testbench selects
            # its test case itself, via a +test_case= plusarg the caller
            # supplies through extra_run_args (matching how ifu_esl_lfsr.v
            # reads it) -- there is nothing for sims itself to select here,
            # so -<sim>_run is passed with no trailing test name.
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
        argv.append(f"-{sim_type}_run")
        if config.sys == "manycore":
            argv.append(test)

        stdout, stderr, rc, wall = _run(
            "sims " + " ".join(shlex.quote(a) for a in argv),
            root,
            config.core,
            run_dir,
            timeout_seconds,
        )

        sim_log = _read_full_if_present(os.path.join(run_dir, "sim.log")) or stdout
        status_log = _read_full_if_present(os.path.join(run_dir, "status.log"))
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
            status_log=_tail(status_log),
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
            results_dir=os.path.join(root, "build", config.sys, config.build_id, "runs"),
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
            patterns: Globs relative to *base_dir*; ``**`` is recursive. A
                pattern containing ``..`` components that would resolve
                outside *base_dir* (e.g. ``../../../../etc/passwd``) matches
                nothing -- *base_dir* is the confinement boundary, not just
                the glob's starting point.
            max_bytes_per_file: Files over this size are recorded in ``skipped``
                rather than shipped -- protects against a glob matching a model
                binary or a multi-megabyte waveform.
        """
        root = _require_root(piton_root)
        base = os.path.normpath(base_dir if os.path.isabs(base_dir) else _resolve_under(root, base_dir))
        files: dict[str, str] = {}
        skipped: dict[str, int] = {}
        listing: dict[str, int] = {}

        for pattern in patterns:
            for path in _glob.glob(os.path.join(base, pattern), recursive=True):
                if not os.path.isfile(path):
                    continue
                path = os.path.normpath(path)
                if path != base and not path.startswith(base + os.sep):
                    logger.warning(
                        "collect: pattern %r matched outside base_dir %r -- skipped", pattern, base
                    )
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
        model_dir = os.path.join(root, "build", config.sys, config.build_id)
        if not os.path.isdir(model_dir):
            return False
        shutil.rmtree(model_dir, ignore_errors=True)
        logger.info("removed %s", model_dir)
        return True
