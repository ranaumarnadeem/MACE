"""chia_openpiton.state_def — result/artifact dataclasses for the OpenPiton nodes.

OpenPiton drives every flow through its own ``sims`` Perl tool, which builds a
simulation model under ``$PITON_ROOT/build/manycore/<build_id>/`` and later
commands read what earlier ones wrote there. That directory is PATH-BASED
state on the worker, so chained calls (configure -> build -> run) must land on
the SAME worker; :class:`~chia_openpiton.openpiton_workspace.OpenPitonWorkspaceNode`
enforces that with a placement group.

Two OpenPiton-specific facts shape these types:

* **Exit code is not success.** An RTL simulation exits 0 whether or not the
  program passed. The verdict comes from the testbench monitor's transcript
  (``Simulation -> PASS (HIT GOOD TRAP)``), so :class:`PitonRunResult` carries a
  tri-state ``verdict`` and derives ``success`` from it, never from ``returncode``.
* **Builds must not collide.** ``sims`` defaults every model to ``rel-0.1``, so
  two configurations in one checkout overwrite each other. :attr:`PitonConfig.key`
  hashes everything that changes the produced model and feeds ``-build_id``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

# Cores this adapter supports. OpenPiton also ships a PicoRV32 ("pico") tile,
# but it needs a 32-bit toolchain nothing in the OpenPiton repo installs and
# has no Verilator run job in OpenPiton's own CI, so it is out of scope.
PitonCore = Literal["ariane", "sparc"]

# sims simulator selectors (-<sim>_build / -<sim>_run / -sim_type=<sim>).
# Only "vlt" (Verilator) is license-free; the rest need commercial tools.
SimType = Literal["vlt", "vcs", "ncv", "icv", "msm", "riv"]

# The only two values sims accepts for -network_config. Note that leaving it
# unset makes sims default to the string "2d_mesh", which is a THIRD spelling
# that pyhplib.py does not recognise -- so we always pass this explicitly.
NetworkConfig = Literal["2dmesh_config", "xbar_config"]

# Outcome of a simulation, parsed from the transcript. See parse.sim_verdict.
Verdict = Literal["pass", "fail", "timeout", "maxcycles"]

SIM_TYPES: frozenset[str] = frozenset(("vlt", "vcs", "ncv", "icv", "msm", "riv"))

# sims enforces this in Perl: "DIE. x_tiles can be at most 256".
MAX_TILES_PER_AXIS = 256

# Cache geometry defaults, mirroring piton/tools/src/sims/manycore.config.
# Keys are the sims flag suffixes: -config_<key>_size / -config_<key>_associativity.
DEFAULT_CACHES: dict[str, tuple[int, int]] = {
    "l1i": (16384, 4),
    "l1d": (8192, 4),
    "l15": (8192, 4),
    "l2": (65536, 4),
}


@dataclass(frozen=True)
class PitonConfig:
    """A resolved OpenPiton configuration and the identity that caches it.

    ``key`` is a content hash of everything that changes the produced model:
    the source revisions, the core, the mesh, the NoC topology, cache geometry,
    RTL defines, extra flags, the Verilator version, and the diff of any file
    edits ``configure`` wrote into the checkout. Two configs with the same key
    produce the same model, so a build may be served from cache; any difference
    yields a different ``build_id`` and therefore a separate model directory.
    """

    core: PitonCore = "ariane"
    x_tiles: int = 1
    y_tiles: int = 1
    network_config: NetworkConfig = "2dmesh_config"
    config_rtl: tuple[str, ...] = ("MINIMAL_MONITORING",)
    caches: dict[str, tuple[int, int]] = field(default_factory=lambda: dict(DEFAULT_CACHES))
    extra_flags: tuple[str, ...] = ()
    # Filled in by configure() on the worker; part of the identity because they
    # change the produced model even when no flag changed.
    source_rev: str = ""
    ariane_rev: str = ""
    verilator_version: str = ""
    # Unified diff of file-level edits configure() wrote into the checkout
    # (e.g. a replacement device address map). "" when the config is flags-only.
    diff: str = ""

    def __post_init__(self) -> None:
        if self.core not in ("ariane", "sparc"):
            raise ValueError(f"core must be 'ariane' or 'sparc', got {self.core!r}")
        for axis, n in (("x_tiles", self.x_tiles), ("y_tiles", self.y_tiles)):
            if not isinstance(n, int) or isinstance(n, bool):
                raise ValueError(f"{axis} must be an int, got {n!r}")
            if not 1 <= n <= MAX_TILES_PER_AXIS:
                raise ValueError(f"{axis} must be 1..{MAX_TILES_PER_AXIS}, got {n}")
        if self.network_config not in ("2dmesh_config", "xbar_config"):
            raise ValueError(
                "network_config must be '2dmesh_config' or 'xbar_config', "
                f"got {self.network_config!r}"
            )
        for name, geom in self.caches.items():
            if name not in DEFAULT_CACHES:
                raise ValueError(
                    f"unknown cache {name!r}; valid: {sorted(DEFAULT_CACHES)}"
                )
            size, assoc = geom
            if size <= 0 or assoc <= 0:
                raise ValueError(f"cache {name} size/associativity must be positive, got {geom}")

    @property
    def num_tiles(self) -> int:
        return self.x_tiles * self.y_tiles

    @property
    def key(self) -> str:
        """Stable content hash of this configuration."""
        identity = {
            "core": self.core,
            "x_tiles": self.x_tiles,
            "y_tiles": self.y_tiles,
            "network_config": self.network_config,
            "config_rtl": sorted(self.config_rtl),
            "caches": {k: list(v) for k, v in sorted(self.caches.items())},
            "extra_flags": list(self.extra_flags),
            "source_rev": self.source_rev,
            "ariane_rev": self.ariane_rev,
            "verilator_version": self.verilator_version,
            "diff": self.diff,
        }
        blob = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    @property
    def build_id(self) -> str:
        """``-build_id`` value: one model directory per distinct configuration."""
        return f"mace_{self.key[:12]}"

    @property
    def finish_mask(self) -> str:
        """Default ``+finish_mask`` for this mesh: one '1' per tile.

        A multi-tile run only passes when every hart hits the good trap, so the
        mask must have a digit per tile (OpenPiton's CI uses 16 ones for 4x4).
        """
        return "1" * self.num_tiles

    def sims_flags(self) -> tuple[str, ...]:
        """The ``sims`` arguments this configuration implies, in a stable order."""
        flags: list[str] = [
            "-sys=manycore",
            f"-x_tiles={self.x_tiles}",
            f"-y_tiles={self.y_tiles}",
            f"-network_config={self.network_config}",
        ]
        if self.core == "ariane":
            flags.append("-ariane")
        for unit in self.config_rtl:
            flags.append(f"-config_rtl={unit}")
        for name in sorted(self.caches):
            size, assoc = self.caches[name]
            flags.append(f"-config_{name}_size={size}")
            flags.append(f"-config_{name}_associativity={assoc}")
        flags.extend(self.extra_flags)
        return tuple(flags)


@dataclass
class PitonBuildArtifact:
    """Result of one ``sims ... -<sim>_build``."""

    success: bool
    returncode: int  # -1 on timeout
    config: PitonConfig
    sim_type: str
    model_dir: str  # $PITON_ROOT/build/manycore/<build_id>
    binary_path: str  # <model_dir>/obj_dir/Vcmp_top; "" when the build failed
    wall_time_s: float
    verilator_version: str = ""
    cache_key: str = ""
    failure_reason: str = ""  # parse.build_failure_reason, "" on success
    stdout: str = ""  # capped tail
    stderr: str = ""  # capped tail


@dataclass
class PitonRunResult:
    """Result of one ``sims ... -<sim>_run <test>``.

    ``success`` never consults ``returncode`` alone: the simulator exits 0 on a
    failing program. It requires the transcript verdict to be ``"pass"``.
    """

    success: bool
    returncode: int  # -1 on timeout
    test: str
    sim_type: str
    run_dir: str
    verdict: Verdict | None = None  # None when nothing matched (parse failure)
    # $time stamped on the verdict line. This, not `cycles`, is what these
    # configurations actually report: the status.log regreport writes for them
    # carries no Cyc= field.
    sim_time: int | None = None
    cycles: int | None = None
    exec_cycles: int | None = None
    wall_time_s: float = 0.0
    sim_log_tail: str = ""
    status_log: str = ""
    fake_uart: str = ""  # what the program printed to the UART
    stdout: str = ""
    stderr: str = ""

    @staticmethod
    def decide(returncode: int, verdict: Verdict | None) -> bool:
        """The one place the pass rule lives."""
        return returncode != -1 and verdict == "pass"


@dataclass
class PitonRegressResult:
    """Result of one regression group (many :class:`PitonRunResult`)."""

    success: bool
    group: str
    sim_type: str
    num_tests: int
    num_failures: int
    results: list[PitonRunResult] = field(default_factory=list)
    results_dir: str = ""
    report: str = ""


@dataclass
class PitonCollectResult:
    """Text files fetched by value from a workspace."""

    base_dir: str
    files: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)  # over the cap; size shown
    listing: dict[str, int] = field(default_factory=dict)
