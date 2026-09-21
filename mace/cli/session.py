"""mace.cli.session -- interactive-shell state, and the two honesty checks
the whole CLI design leans on: which cores actually have an adapter, and
which mesh shapes this project actually has real evidence for.

One Session accumulates state across `read_verilog`, `top_module`,
`read_spec`, and `set_core` -- matching how a real EDA tool's interactive
shell works (Yosys, OpenROAD): state builds up across commands, and a later
command (`run`) acts on everything accumulated so far, rather than each
command being a fully self-contained operation the way mace's own
non-interactive example scripts are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from chia_openpiton.state_def import MAX_TILES_PER_AXIS, PitonCore

# Cores chia_openpiton actually has a working L15 adapter for -- the real
# boundary this project's own research established (docs/TECHNICAL_GUIDE.md
# section 1: OpenPiton has no generic core-to-NoC bridge, every core needs a
# hand-written adapter, and these are the only ones that exist). Matched by
# case-insensitive substring against a declared top_module name, including a
# couple of the cores' own upstream project names.
SUPPORTED_CORES: dict[str, PitonCore] = {
    "ariane": "ariane",
    "cva6": "ariane",
    "sparc": "sparc",
    "opensparc": "sparc",
    "picorv32": "pico",
    "pico": "pico",
}


def detect_core(top_module: str) -> PitonCore | None:
    """Which supported core *top_module* names, or ``None`` if it names none
    of them.

    Deliberately a name match, not RTL analysis: chia_openpiton has no
    generic way to inspect a core's own memory interface and decide whether
    an L15 adapter could exist for it -- that judgment call is exactly what
    this project's own PicoRV32 investigation needed real work to make (see
    docs/TECHNICAL_GUIDE.md). A name that matches none of the known cores is
    treated as "no adapter exists for this", not "let me go check the RTL" --
    which is exactly the honest, immediate answer mace.report's post-mortem
    mechanism exists to explain, not something worth faking a real build
    attempt over.
    """
    lowered = top_module.lower()
    for needle, core in SUPPORTED_CORES.items():
        if needle in lowered:
            return core
    return None


# What this project actually knows about each total tile count it has real
# evidence for -- see docs/TECHNICAL_GUIDE.md section 7 for the full account
# behind each of these. Anything not listed here is accepted (chia_openpiton
# itself places no restriction beyond MAX_TILES_PER_AXIS per side) but
# printed as genuinely unvalidated, not silently treated the same as 1 or 16.
KNOWN_MESH_OUTCOMES: dict[int, str] = {
    1: "validated -- passes repeatedly on real hardware",
    4: (
        "builds successfully on real hardware, but the run hangs -- likely a "
        "genuine RTL gap in an untested mesh shape (2x2 has no upstream "
        "Verilator precedent), not a configuration problem"
    ),
    16: (
        "the one multi-tile shape upstream itself has Verilator-validated -- "
        "not yet completed end to end in this project, see "
        "docs/TECHNICAL_GUIDE.md section 7"
    ),
}


def mesh_for_core_count(n: int) -> tuple[int, int]:
    """A reasonable ``(x_tiles, y_tiles)`` for *n* total tiles.

    Prefers a square mesh when *n* is a perfect square -- every mesh this
    project has ever actually built is square (1x1, 2x2, 4x4) -- otherwise
    the narrowest rectangle that fits, since chia_openpiton itself has no
    preference beyond what OpenPiton's own mesh topology allows.

    Raises ``ValueError`` if no mesh of *n* tiles could fit within
    ``MAX_TILES_PER_AXIS`` per side -- checked before the trial-division
    loop below (not just on its result), since *n* itself is otherwise
    unbounded and this project's own hard limit (a per-axis cap of 256)
    means no valid *n* ever exceeds ``MAX_TILES_PER_AXIS ** 2`` tiles; an
    absurd *n* (e.g. an accidental extra digit typed into `set_core`)
    would otherwise make this loop scan up to sqrt(n) candidates, which
    is a real hang risk in an interactive shell for a large enough typo.
    """
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        raise ValueError(f"core count must be a positive int, got {n!r}")
    if n > MAX_TILES_PER_AXIS * MAX_TILES_PER_AXIS:
        raise ValueError(
            f"core count {n} cannot fit in any mesh up to "
            f"{MAX_TILES_PER_AXIS}x{MAX_TILES_PER_AXIS} "
            f"({MAX_TILES_PER_AXIS * MAX_TILES_PER_AXIS} tiles)"
        )
    root = int(n**0.5)
    if root * root == n:
        mesh = (root, root)
    else:
        mesh = next(((n // y, y) for y in range(root, 0, -1) if n % y == 0), (n, 1))
    x, y = mesh
    if x > MAX_TILES_PER_AXIS or y > MAX_TILES_PER_AXIS:
        raise ValueError(
            f"core count {n} only factors into a {x}x{y} mesh, "
            f"which exceeds the {MAX_TILES_PER_AXIS}-tile-per-axis limit"
        )
    return mesh


@dataclass
class Session:
    """Everything accumulated in one shell session (or one non-interactive
    `mace run` invocation) before `run` executes."""

    piton_root: str
    # Set by read_verilog after it confirms each path exists -- validation
    # only. Nothing downstream (build_spec_from_session, MaceSpec, the
    # actual build) consumes this; see handle_read_verilog's own docstring.
    verilog_files: tuple[Path, ...] = ()
    top_module: str | None = None
    objective: str | None = None
    workloads: tuple[str, ...] = ()
    core_count: int | None = None
    verbose: bool = True  # logging should be verbose -- see shell.py
    coverage: bool = False  # set by `run -coverage`; sticks until toggled again
    last_result: object = None  # mace.spec.LoopResult, once `run` has happened
    last_coverage: dict | None = None  # {"hit", "total", "percent"}, once computed

    @property
    def detected_core(self) -> PitonCore | None:
        return detect_core(self.top_module) if self.top_module else None

    @property
    def target_mesh(self) -> tuple[int, int] | None:
        # `is not None`, not truthiness: 0 is a genuinely invalid core count
        # (mesh_for_core_count itself rejects it) and must reach that
        # validation, not be silently treated the same as "never set" --
        # see handle_set_core's own ValueError handling in shell.py.
        return mesh_for_core_count(self.core_count) if self.core_count is not None else None
