"""Core descriptors for the generic tile.

A core descriptor is a short YAML file that says what a RISC-V core needs to
run on the generic tile (scripts/patch_openpiton.sh fixes 14-17): where its
RTL is, its ISA, which bus its wrapper presents, its reset vector, and what
it has that the tile cannot support. :func:`support_problems` checks it
before any build, and the generators write the two files the build reads:
``tile/generic/rtl/Flist.core`` (the core's sources and wrapper) and the
generic test environment's ``link.ld`` (the reset vector).

Example::

    core: ibex
    top_module: ibex_top
    wrapper: ibex/generic_core_wrapper.sv
    sources: [ibex/rtl/ibex_pkg.sv, ibex/rtl/ibex_top.sv]
    include_dirs: [ibex/vendor/lowrisc_ip/ip/prim/rtl]
    isa: {march: rv32imc, mabi: ilp32}
    bus: obi
    reset_vector: 0x40000080
    atomics: none
    private_cache: none
    hart_id: register

Paths are relative to ``piton/design/chip/tile/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass

import yaml

BUSES = ("simple", "obi", "wishbone")
ATOMICS = ("none", "bus")
PRIVATE_CACHES = ("none", "disabled", "write_through", "write_back")
HART_IDS = ("register", "port")

# rv32_as loads every program image at this address.
RAM_BASE = 0x4000_0000
# The default link script puts .text at the first 4 KB boundary after
# .text.init, so the reset vector must fall in the first 4 KB of RAM.
RESET_WINDOW = 0x1000

TILE_DIR = os.path.join("piton", "design", "chip", "tile")
FLIST_CORE = os.path.join(TILE_DIR, "generic", "rtl", "Flist.core")
LINK_LD = os.path.join("piton", "verif", "diag", "assembly", "include", "riscv", "generic", "link.ld")

# Tests for each ladder step after boot, in the generic test environment.
ISA_TESTS = ("addi.S", "beq.S", "jal.S", "lw.S", "sw.S", "lb.S", "sb.S", "lh.S", "sh.S")
M_TESTS = ("mul.S", "div.S")
# The store tests write different values to one shared word in different test
# cases, so on a mesh one tile's store can land between another tile's store
# and load. A mesh runs the ISA tests that keep their stores to themselves.
MESH_TESTS = ("addi.S", "beq.S", "jal.S", "lw.S")
SHARED_TESTS = ("generic_tile_id.S", "generic_shared_flag.S")
ATOMIC_TESTS = ("generic_amo_counter.S",)

_NAME = re.compile(r"[a-z][a-z0-9_]*")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_MARCH = re.compile(r"rv(32|64|128)([a-z]+)((_[a-z0-9]+)*)")
_FIELDS = (
    "core", "top_module", "wrapper", "sources", "include_dirs", "isa", "bus", "reset_vector",
    "ram_base", "atomics", "private_cache", "harts", "own_interconnect", "hart_id", "notes",
)
_REQUIRED = ("core", "top_module", "wrapper", "sources", "isa", "bus", "reset_vector", "atomics")


@dataclass(frozen=True)
class CoreDescriptor:
    """A validated core descriptor. Construct it with :func:`parse_descriptor`
    or :func:`load_descriptor`; ``__post_init__`` rejects malformed values
    with ``ValueError``. A well-formed descriptor may still describe a core
    the generic tile cannot run: :func:`support_problems` says why."""

    core: str
    top_module: str
    wrapper: str
    sources: tuple[str, ...]
    march: str
    mabi: str
    bus: str
    reset_vector: int
    atomics: str
    include_dirs: tuple[str, ...] = ()
    ram_base: int = RAM_BASE
    private_cache: str = "none"
    harts: int = 1
    own_interconnect: bool = False
    hart_id: str = "register"
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.core, str) or not _NAME.fullmatch(self.core):
            raise ValueError(f"core must be a lower-case name like 'ibex', got {self.core!r}")
        if not isinstance(self.top_module, str) or not _MODULE.fullmatch(self.top_module):
            raise ValueError(f"top_module must be a Verilog module name, got {self.top_module!r}")
        _check_path("wrapper", self.wrapper)
        if not self.sources:
            raise ValueError("sources must list at least one file")
        for path in self.sources:
            _check_path("sources", path)
        for path in self.include_dirs:
            _check_path("include_dirs", path)
        if not isinstance(self.march, str) or not _MARCH.fullmatch(self.march):
            raise ValueError(f"isa.march must look like 'rv32imc', got {self.march!r}")
        if not isinstance(self.mabi, str) or not _NAME.fullmatch(self.mabi):
            raise ValueError(f"isa.mabi must look like 'ilp32', got {self.mabi!r}")
        for name, value, allowed in (
            ("bus", self.bus, BUSES),
            ("atomics", self.atomics, ATOMICS),
            ("private_cache", self.private_cache, PRIVATE_CACHES),
            ("hart_id", self.hart_id, HART_IDS),
        ):
            if value not in allowed:
                raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
        for name in ("reset_vector", "ram_base", "harts"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")
        if self.harts < 1:
            raise ValueError(f"harts must be at least 1, got {self.harts}")
        if not isinstance(self.own_interconnect, bool):
            raise ValueError(f"own_interconnect must be true or false, got {self.own_interconnect!r}")
        if not isinstance(self.notes, str):
            raise ValueError("notes must be text")

    @property
    def extensions(self) -> str:
        """The single-letter ISA extensions after the base, e.g. 'imc'."""
        letters = _MARCH.fullmatch(self.march).group(2)
        return letters.replace("g", "imafd")

    @property
    def digest(self) -> str:
        """Content hash of the descriptor, for the build identity."""
        blob = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def sims_flags(self) -> tuple[str, ...]:
        """The sims flags that assemble test programs for this core's ISA."""
        return (f"-rv32_march={self.march}", f"-rv32_mabi={self.mabi}")

    def ladder_tests(self) -> dict[str, tuple[str, ...]]:
        """Test programs per ladder step, chosen from the ISA and atomics."""
        m_tests = M_TESTS if "m" in self.extensions else ()
        steps = {"isa": ISA_TESTS + m_tests, "mesh": MESH_TESTS + m_tests, "shared": SHARED_TESTS}
        if self.atomics == "bus":
            steps["atomics"] = ATOMIC_TESTS
        return steps


def _check_path(field: str, path: object) -> None:
    if not isinstance(path, str) or not path:
        raise ValueError(f"{field} entries must be non-empty paths, got {path!r}")
    parts = path.replace("\\", "/").split("/")
    if path.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", path) or ".." in parts:
        raise ValueError(f"{field} paths must stay under piton/design/chip/tile/, got {path!r}")


def parse_descriptor(data: dict) -> CoreDescriptor:
    """Build a :class:`CoreDescriptor` from parsed YAML."""
    if not isinstance(data, dict):
        raise ValueError(f"a core descriptor must be a mapping, got {type(data).__name__}")
    unknown = sorted(set(data) - set(_FIELDS))
    if unknown:
        raise ValueError(f"unknown descriptor fields: {', '.join(unknown)}")
    missing = [name for name in _REQUIRED if name not in data]
    if missing:
        raise ValueError(f"missing descriptor fields: {', '.join(missing)}")
    isa = data["isa"]
    if not isinstance(isa, dict) or set(isa) != {"march", "mabi"}:
        raise ValueError(f"isa must be a mapping with march and mabi, got {isa!r}")
    for name in ("sources", "include_dirs"):
        if name in data and not isinstance(data[name], list):
            raise ValueError(f"{name} must be a list, got {data[name]!r}")
    fields = {k: v for k, v in data.items() if k != "isa"}
    fields["sources"] = tuple(data["sources"])
    fields["include_dirs"] = tuple(data.get("include_dirs", ()))
    if fields.get("notes") is None:
        fields["notes"] = ""
    return CoreDescriptor(march=isa["march"], mabi=isa["mabi"], **fields)


def load_descriptor(path: str) -> CoreDescriptor:
    """Read and validate a core descriptor YAML file."""
    with open(path) as f:
        return parse_descriptor(yaml.safe_load(f))


def support_problems(desc: CoreDescriptor) -> tuple[str, ...]:
    """Why the generic tile cannot run this core; empty when it can.

    Checked before any build, so an unsupported core ends with a reason and
    no simulation."""
    problems = []
    xlen = _MARCH.fullmatch(desc.march).group(1)
    if xlen != "32":
        problems.append(f"isa.march {desc.march} is RV{xlen}; the generic tile's bus is 32-bit (RV32 only)")
    if desc.harts != 1:
        problems.append(f"the core has {desc.harts} harts; the generic tile runs one hart per tile")
    if desc.private_cache in ("write_back", "write_through"):
        problems.append(
            f"the core has a {desc.private_cache.replace('_', '-')} private cache the wrapper cannot turn "
            "off; the generic tile has no invalidation path to keep it coherent with the L1.5"
        )
    if desc.own_interconnect:
        problems.append("the core brings its own L2, L3, or NoC; the generic tile connects a core to the "
                        "tile's L1.5 only")
    if desc.ram_base != RAM_BASE:
        problems.append(f"ram_base is {desc.ram_base:#x}; programs are loaded at {RAM_BASE:#x}")
    offset = desc.reset_vector - RAM_BASE
    if not 0 <= offset < RESET_WINDOW or desc.reset_vector % 4:
        problems.append(
            f"reset_vector {desc.reset_vector:#x} must be word-aligned and within the first "
            f"{RESET_WINDOW:#x} bytes of RAM ({RAM_BASE:#x}-{RAM_BASE + RESET_WINDOW - 1:#x})"
        )
    if desc.atomics == "bus" and desc.bus != "simple":
        problems.append(f"atomics: bus needs a bus that carries amo_op; the {desc.bus} bridge does not "
                        "(only bus: simple does)")
    if desc.atomics == "none" and "a" in desc.extensions:
        problems.append(f"isa.march {desc.march} has the A extension but atomics is none, so atomic "
                        "instructions could not reach the L2")
    return tuple(problems)


def flist_core(desc: CoreDescriptor) -> str:
    """``Flist.core``: include directories, sources in order, then the
    wrapper, relative to ``tile/generic/rtl/``."""
    lines = [f"// {desc.core} ({desc.top_module}), written from its core descriptor"]
    lines += [f"+incdir+../../{d}" for d in desc.include_dirs]
    lines += [f"../../{s}" for s in desc.sources if s != desc.wrapper]
    lines.append(f"../../{desc.wrapper}")
    return "\n".join(lines) + "\n"


def link_ld(desc: CoreDescriptor) -> str:
    """The generic test environment's link script for this reset vector."""
    offset = desc.reset_vector - RAM_BASE
    init = f"{{ . = {offset:#x}; *(.text.init) }}" if offset else "{ *(.text.init) }"
    return (
        f"/* {desc.core}: written from its core descriptor. The reset vector is "
        f"{desc.reset_vector:#x}; rv32_as loads the image at {RAM_BASE:#x}. */\n"
        'OUTPUT_ARCH( "riscv" )\n'
        "ENTRY(_start)\n\n"
        "SECTIONS\n{\n"
        f"  . = {RAM_BASE:#x};\n"
        f"  .text.init : {init}\n"
        "  . = ALIGN(0x1000);\n"
        "  .text : { *(.text) }\n"
        "  . = ALIGN(0x1000);\n"
        "  .data : { *(.data) }\n"
        "  .bss : { *(.bss) }\n"
        "  _end = .;\n"
        "}\n"
    )


def missing_files(desc: CoreDescriptor, piton_root: str, wrapper: bool = True) -> tuple[str, ...]:
    """Sources, the wrapper, and include directories the checkout lacks.
    With *wrapper* false the wrapper is not checked, since
    :func:`apply_descriptor` writes a skeleton in its place."""
    tile = os.path.join(piton_root, TILE_DIR)
    files = (*desc.sources, desc.wrapper) if wrapper else tuple(s for s in desc.sources if s != desc.wrapper)
    missing = [p for p in files if not os.path.isfile(os.path.join(tile, p))]
    missing += [d for d in desc.include_dirs if not os.path.isdir(os.path.join(tile, d))]
    return tuple(dict.fromkeys(missing))


def wrapper_skeleton(desc: CoreDescriptor) -> str:
    """A ``generic_core_wrapper`` with the fixed ports and no core: its bus
    is tied off, so a build of it alone fails the boot check. An agent (or a
    person) replaces the marked block with the core and its bridge."""
    bridge = {
        "simple": "no bridge: the core's memory interface is the simple bus",
        "obi": "generic_bridge_obi (tile/generic/rtl/generic_bridge_obi.v)",
        "wishbone": "generic_bridge_wishbone (tile/generic/rtl/generic_bridge_wishbone.v)",
    }[desc.bus]
    lines = [
        f"// generic_core_wrapper for {desc.core} ({desc.top_module}) on the generic tile.",
        f"// Bus: {desc.bus}, through {bridge}. Reset vector: {desc.reset_vector:#010x}.",
        "// The ports are fixed; see generic_l15_transducer.v for the simple bus.",
        "",
        "module generic_core_wrapper (",
        "    input  wire                         clk,",
        "    input  wire                         rst_n,",
        "    input  wire [31:0]                  hart_id,",
        "",
        "    output wire                         bus_valid,",
        "    output wire [31:0]                  bus_addr,",
        "    output wire [3:0]                   bus_wstrb,",
        "    output wire [31:0]                  bus_wdata,",
        "    output wire [`L15_AMO_OP_WIDTH-1:0] bus_amo_op,",
        "    input  wire                         bus_ready,",
        "    input  wire [31:0]                  bus_rdata,",
        "    input  wire                         wake",
        ");",
        "",
        f"    // {SKELETON_MARK}: replace this block with the {desc.top_module} instance and its bridge.",
        "    assign bus_valid  = 1'b0;",
        "    assign bus_addr   = 32'b0;",
        "    assign bus_wstrb  = 4'b0;",
        "    assign bus_wdata  = 32'b0;",
        "    assign bus_amo_op = `L15_AMO_OP_NONE;",
        f"    // end of {SKELETON_MARK}",
        "",
        "endmodule",
    ]
    return "\n".join(lines) + "\n"


SKELETON_MARK = "wrapper body"


def apply_descriptor(desc: CoreDescriptor, piton_root: str) -> tuple[str, ...]:
    """Write ``Flist.core`` and ``link.ld`` for this core into a checkout
    patched with fixes 14-17, and :func:`wrapper_skeleton` in place of a
    missing wrapper. Returns the paths written, relative to the checkout.
    Raises ``ValueError`` for an unsupported core or missing files, before
    writing anything."""
    problems = support_problems(desc)
    if problems:
        raise ValueError("unsupported core: " + "; ".join(problems))
    missing = missing_files(desc, piton_root, wrapper=False)
    if missing:
        raise ValueError(f"files not found under {TILE_DIR}: {', '.join(missing)}")
    files = [(FLIST_CORE, flist_core(desc)), (LINK_LD, link_ld(desc))]
    for rel, _ in files:
        if not os.path.isdir(os.path.join(piton_root, os.path.dirname(rel))):
            raise ValueError(f"{os.path.dirname(rel)} not found: patch the checkout with fixes 14-17 first")
    wrapper = os.path.join(TILE_DIR, desc.wrapper).replace(os.sep, "/")
    if not os.path.isfile(os.path.join(piton_root, wrapper)):
        files.append((wrapper, wrapper_skeleton(desc)))
    for rel, text in files:
        path = os.path.join(piton_root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    return tuple(rel for rel, _ in files)


def boot_problems(desc: CoreDescriptor, seen: dict[int, int], num_tiles: int) -> tuple[str, ...]:
    """Ladder step 2: every tile's core must make its first request at the
    descriptor's reset vector. *seen* is each tile's first request address
    (:func:`chia_openpiton.parse.first_requests`). Empty when all of them did."""
    problems = []
    for tile in range(num_tiles):
        if tile not in seen:
            problems.append(f"tile {tile}: the core made no memory request after reset; check its clock, "
                            "its reset polarity, and any fetch-enable input")
        elif seen[tile] != desc.reset_vector:
            problems.append(f"tile {tile}: the core's first request was at {seen[tile]:#010x}, not the "
                            f"descriptor's reset_vector {desc.reset_vector:#010x}")
    return tuple(problems)


def main(argv: list[str] | None = None) -> int:
    """``python -m chia_openpiton.core_descriptor check <yaml> [piton_root]``
    prints why a core is unsupported; ``apply <yaml> <piton_root>`` writes
    its Flist.core and link.ld. Both exit 1 on a problem."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m chia_openpiton.core_descriptor")
    ap.add_argument("action", choices=("check", "apply"))
    ap.add_argument("descriptor")
    ap.add_argument("piton_root", nargs="?")
    args = ap.parse_args(argv)
    try:
        desc = load_descriptor(args.descriptor)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"{args.descriptor}: {e}")
        return 1
    if args.action == "apply":
        if not args.piton_root:
            ap.error("apply needs a piton_root")
        try:
            written = apply_descriptor(desc, args.piton_root)
        except ValueError as e:
            print(e)
            return 1
        print("wrote " + ", ".join(written))
        return 0
    problems = list(support_problems(desc))
    if args.piton_root:
        missing = missing_files(desc, args.piton_root)
        if missing:
            problems.append(f"files not found under {TILE_DIR}: {', '.join(missing)}")
    for p in problems:
        print(f"unsupported: {p}")
    if not problems:
        print(f"{desc.core}: supported")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
