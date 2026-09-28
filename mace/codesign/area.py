"""mace.codesign.area -- a design's cache area, from CHIA's SRAM models.

Each cache is modelled as two SRAM arrays: a data array with one line per
row, and a tag array with one set per row and one tag per way, so its width,
and its area, grow with associativity. A tag holds the address bits above
the set index and line offset, plus two state bits. Line sizes and the
40-bit physical address follow OpenPiton's defaults; the technology is
32 nm, OpenPiton's tape-out node.

With a CACTI 7 binary named by ``MACE_CACTI`` or found as ``cacti`` on
``PATH``, each array goes through CHIA's ``run_cacti``; otherwise, and for
any array CACTI rejects, through CHIA's ``analytical_area_estimate``, one
square micron per bit. The two scales differ, so a comparison uses one
source throughout; :func:`design_area` reports which one it used.
"""

from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass
from functools import lru_cache

from chia.chipyard.macrocompiler import SRAMSpec
from chia.vlsi.sram_cacti.cacti_runner import analytical_area_estimate, run_cacti

from chia_openpiton.state_def import DEFAULT_CACHES
from mace.codesign.space import Design

LINE_BYTES = {"l1i": 16, "l1d": 16, "l15": 16, "l2": 64}
ADDRESS_BITS = 40
STATE_BITS = 2
TECHNOLOGY_UM = 0.032


@dataclass(frozen=True)
class Area:
    """Cache area of a whole design, over every tile."""

    area_um2: float
    read_energy_nj: float
    source: str  # "cacti", "analytical", or "mixed"


def cacti_path() -> str | None:
    """The CACTI binary to use, or ``None`` when there is none."""
    return os.environ.get("MACE_CACTI") or shutil.which("cacti")


def cache_arrays(name: str, size: int, assoc: int) -> tuple[SRAMSpec, SRAMSpec]:
    """The data and tag arrays of one cache.

    Raises:
        ValueError: *size* holds less than one set of *assoc* lines.
    """
    line = LINE_BYTES[name]
    sets = size // (line * assoc)
    if sets < 1 or size % (line * assoc):
        raise ValueError(f"{name}: {size} bytes is not a whole number of {assoc}-way sets of {line}-byte lines")
    tag_bits = ADDRESS_BITS - int(math.log2(sets)) - int(math.log2(line)) + STATE_BITS
    data = SRAMSpec(name=f"{name}_data", depth=size // line, width=line * 8, ports="rw", mask_gran=None, num_rw_ports=1)
    tag = SRAMSpec(name=f"{name}_tag", depth=sets, width=assoc * tag_bits, ports="rw", mask_gran=None, num_rw_ports=1)
    return data, tag


@lru_cache(maxsize=None)
def _array_area(depth: int, width: int, name: str, cacti: str | None) -> tuple[float, float, str]:
    spec = SRAMSpec(name=name, depth=depth, width=width, ports="rw", mask_gran=None, num_rw_ports=1)
    if cacti:
        result = run_cacti(spec, technology_um=TECHNOLOGY_UM, cacti_path=cacti)
        if result is not None:
            return result.area_um2, result.read_energy_nj, "cacti"
    result = analytical_area_estimate(spec)
    return result.area_um2, result.read_energy_nj, "analytical"


def design_area(design: Design, tiles: int, cacti: str | None = None) -> Area:
    """The cache area of *design* on *tiles* tiles.

    *cacti* defaults to :func:`cacti_path`. Raises ``ValueError`` for a
    cache geometry that is not a whole number of sets.
    """
    cacti = cacti_path() if cacti is None else (cacti or None)
    area = energy = 0.0
    sources = set()
    for name, (size, assoc) in design.caches:
        for spec in cache_arrays(name, size, assoc):
            a, e, source = _array_area(spec.depth, spec.width, spec.name, cacti)
            area += a
            energy += e
            sources.add(source)
    source = sources.pop() if len(sources) == 1 else "mixed"
    return Area(area_um2=area * tiles, read_energy_nj=energy * tiles, source=source)


def cache_area_um2(name: str, size: int, assoc: int, tiles: int, cacti: str | None = None) -> float:
    """The area of one cache over *tiles* tiles; *cacti* as in :func:`design_area`."""
    cacti = cacti_path() if cacti is None else (cacti or None)
    return tiles * sum(_array_area(s.depth, s.width, s.name, cacti)[0] for s in cache_arrays(name, size, assoc))


def area_notes(space, tiles: int, cacti: str | None = None) -> str:
    """Each cache geometry's area in *space*, over *tiles* tiles, one line
    per cache: the table the LLM proposer adds up to stay within budget."""
    lines = []
    for name in sorted(DEFAULT_CACHES):
        if name in space.sizes:
            options = [(size, assoc) for size in space.sizes[name] for assoc in space.assocs[name]]
        else:
            options = [DEFAULT_CACHES[name]]
        cells = []
        for size, assoc in options:
            try:
                cells.append(f"{size},{assoc}={cache_area_um2(name, size, assoc, tiles, cacti):.0f}")
            except ValueError:
                cells.append(f"{size},{assoc}=invalid")
        fixed = "" if name in space.sizes else " (fixed)"
        lines.append(f"- {name}{fixed}: " + " ".join(cells))
    return "\n".join(lines)
