"""mace.codesign.rtl -- RTL parameters that OpenPiton's ``sims`` does not expose.

``sims`` sets cache sizes, associativities, the mesh and the network. The
buffer depths, queue sizes and MSHR counts below live in RTL headers and
packages, so a search changes them by editing the source: each parameter lists
the text that holds its value, and :func:`edits_for` rewrites exactly that text
in a checkout. Every ``old`` text must occur exactly once in its file, so a
checkout that differs from the one the catalog was written against fails loudly
instead of building the wrong design.

Edits go to the ``.pyv`` sources and never to the ``.tmp.h`` and ``.tmp.v``
files PyHP writes from them, which every build overwrites. A value that a
header and a PyHP block both hold, such as an L2 MSHR count, is listed once per
copy. :meth:`RtlParam.generated_checks` names the generated files a build must
show the new value in, so a header that is not regenerated cannot pass as a
design with no effect.

A design is the value of every parameter in its space. Its storage cost is the
flip-flop bits it adds per tile over the default design, from the entry widths
in each parameter's ``bits_note``; it orders designs by cost and is not a
synthesis result.
"""

from __future__ import annotations

import itertools
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

FAULT_ROOT = "piton/design"


def _log2(value: int) -> int:
    if value <= 0 or value & (value - 1):
        raise ValueError(f"{value} is not a power of two")
    return value.bit_length() - 1


@dataclass(frozen=True)
class Edit:
    """One place a parameter's value is written.

    *template* is the text with ``{value}`` where the value goes and ``{log2}``
    where its base-2 logarithm goes. Its text at the default value must occur
    exactly once in *path*. *generated* names the file PyHP writes from *path*,
    when the build must show the new text there.
    """

    path: str
    template: str
    generated: str = ""

    def text(self, value) -> str:
        log2 = _log2(value) if isinstance(value, int) and "{log2}" in self.template else 0
        return self.template.format(value=value, log2=log2)


@dataclass(frozen=True)
class RtlParam:
    """A parameter, the values a search may give it, and where its value lives."""

    name: str
    description: str
    default: int | str
    values: tuple
    edits: tuple[Edit, ...]
    # Flip-flop bits one unit of the value adds per tile; 0 for a parameter
    # that adds no storage.
    bits_per_unit: int = 0
    bits_note: str = ""

    def __post_init__(self) -> None:
        if self.default not in self.values:
            raise ValueError(f"{self.name}: the default {self.default!r} is not among its values")
        for edit in self.edits:
            if not edit.path.startswith(FAULT_ROOT + "/"):
                raise ValueError(f"{self.name}: {edit.path} is not under {FAULT_ROOT}/")

    def storage_bits(self, value) -> int:
        return self.bits_per_unit * value if isinstance(value, int) else 0


_L2H = "piton/design/include/l2.h.pyv"
_L2H_GENERATED = "piton/design/include/l2.tmp.h"
_L2_RTL = "piton/design/chip/tile/l2/rtl/"
_ARIANE_INC = "piton/design/chip/tile/ariane/core/include/"

CATALOG: dict[str, RtlParam] = {
    p.name: p
    for p in (
        RtlParam(
            name="l2_mshr",
            description=(
                "Entries in each L2 slice's miss status table: how many misses and coherence transactions "
                "a slice has outstanding at once."
            ),
            default=8,
            values=(4, 8, 16),
            edits=(
                Edit(_L2H, "`define L2_MSHR_ENTRIES         {value}", _L2H_GENERATED),
                Edit(_L2H, "`define L2_MSHR_INDEX_WIDTH     {log2}", _L2H_GENERATED),
                Edit(_L2_RTL + "l2_mshr_wrap.v.pyv", "L2_MSHR_ENTRIES = {value}"),
                Edit(_L2_RTL + "l2_mshr_wrap.v.pyv", "L2_MSHR_INDEX_WIDTH = {log2}"),
                Edit(_L2_RTL + "l2_mshr.v.pyv", "L2_MSHR_ENTRIES = {value}"),
                Edit(_L2_RTL + "l2_mshr.v.pyv", "L2_MSHR_INDEX_WIDTH = {log2}"),
            ),
            bits_per_unit=154,
            bits_note="state 2 + array 138 (L2_MSHR_ARRAY_WIDTH) + owner counter 6 + CAM address 8, from l2.h.pyv",
        ),
        RtlParam(
            name="l2_req_buf",
            description=(
                "Header flits an L2 slice holds for requests from the L1.5 caches before its pipeline takes "
                "them; a request has up to three header flits."
            ),
            default=8,
            values=(4, 8, 16),
            edits=(
                Edit(_L2H, "`define L2_P1_HEADER_BUF_IN_SIZE        {value}", _L2H_GENERATED),
                Edit(_L2H, "`define L2_P1_HEADER_BUF_IN_LOG_SIZE    {log2}", _L2H_GENERATED),
                Edit(_L2_RTL + "l2_pipe1_buf_in.v.pyv", "L2_P1_HEADER_BUF_IN_SIZE = {value}"),
            ),
            bits_per_unit=64,
            bits_note="one NOC_DATA_WIDTH flit per entry (header_buf_mem_f in l2_pipe1_buf_in.v.pyv)",
        ),
        RtlParam(
            name="l2_req_data_buf",
            description="Data flits an L2 slice holds for those requests before its pipeline takes them.",
            default=4,
            values=(2, 4, 8),
            edits=(
                Edit(_L2H, "`define L2_P1_DATA_BUF_IN_SIZE          {value}", _L2H_GENERATED),
                Edit(_L2H, "`define L2_P1_DATA_BUF_IN_LOG_SIZE      {log2}", _L2H_GENERATED),
                Edit(_L2_RTL + "l2_pipe1_buf_in.v.pyv", "L2_P1_DATA_BUF_IN_SIZE = {value}"),
            ),
            bits_per_unit=64,
            bits_note="one NOC_DATA_WIDTH flit per entry (data_buf_mem_f in l2_pipe1_buf_in.v.pyv)",
        ),
        RtlParam(
            name="dcache_wbuf",
            description=(
                "Entries in each Ariane core's write-through data cache write buffer, which merges stores "
                "before they go to the L1.5."
            ),
            default=8,
            values=(4, 8, 16),
            edits=(Edit(_ARIANE_INC + "wt_cache_pkg.sv", "localparam DCACHE_WBUF_DEPTH       = {value};"),),
            bits_per_unit=147,
            bits_note=(
                "wbuffer_t at the default 8 KB 4-way L1D: tag 53 + data 64 + user 1 + dirty 8 + valid 8 "
                "+ txblock 8 + checked 1 + hit_oh 4"
            ),
        ),
        RtlParam(
            name="store_buffer",
            description=(
                "Entries in each Ariane core's speculative and committed store queues; one value sets both."
            ),
            default=4,
            values=(2, 4, 8),
            edits=(
                Edit(_ARIANE_INC + "ariane_pkg.sv", "localparam int unsigned DEPTH_SPEC   = {value};"),
                Edit(_ARIANE_INC + "ariane_pkg.sv", "localparam int unsigned DEPTH_COMMIT = {value};"),
            ),
            bits_per_unit=262,
            bits_note="two queues of 131-bit entries: address 56 + data 64 + byte enable 8 + size 2 + valid 1",
        ),
        RtlParam(
            name="scoreboard",
            description="Instructions an Ariane core can have in flight, as scoreboard entries.",
            default=8,
            values=(4, 8, 16),
            edits=(Edit(_ARIANE_INC + "ariane_pkg.sv", "localparam NR_SB_ENTRIES = {value};"),),
            bits_per_unit=362,
            bits_note=(
                "scoreboard_entry_t at 8 entries: pc 64 + trans_id 3 + fu 4 + op 8 + three registers 18 "
                "+ result 64 + four flags 4 + exception 129 + branch prediction 67 + compressed 1"
            ),
        ),
        RtlParam(
            name="home_alloc",
            description=(
                "Which address bits choose the L2 slice that homes a line: the LOW, MIDDLE or HIGH order bits "
                "above the line offset, or a MIXED selection."
            ),
            default="MIXED",
            values=("LOW", "MIDDLE", "HIGH", "MIXED"),
            edits=(
                Edit(
                    "piton/design/chip/tile/rtl/config_regs.v.pyv",
                    "home_alloc_method <= `HOME_ALLOC_{value}_ORDER_BITS;",
                ),
            ),
            bits_note="the reset value of a register that already exists",
        ),
    )
}


@dataclass(frozen=True)
class RtlDesign:
    """The value of every parameter in a space, as sorted ``(name, value)`` pairs."""

    values: tuple[tuple[str, int | str], ...]

    @classmethod
    def of(cls, choices: dict, names: tuple[str, ...] | None = None) -> "RtlDesign":
        """A design from *choices*; a parameter left out takes its default.
        *names* limits the design to those parameters (all of the catalog if None)."""
        names = tuple(CATALOG) if names is None else names
        unknown = set(choices) - set(CATALOG)
        if unknown:
            raise ValueError(f"unknown RTL parameters {sorted(unknown)}; valid: {sorted(CATALOG)}")
        return cls(tuple(sorted((n, choices.get(n, CATALOG[n].default)) for n in names)))

    def as_dict(self) -> dict:
        return dict(self.values)

    def changed(self) -> dict:
        """The parameters not at their default value."""
        return {n: v for n, v in self.values if v != CATALOG[n].default}

    def describe(self) -> str:
        """``l2_mshr=8 dcache_wbuf=8``: the form the LLM proposer reads and writes."""
        return " ".join(f"{n}={v}" for n, v in self.values)

    def as_json(self) -> str:
        return json.dumps({"rtl": self.as_dict()}, sort_keys=True)

    def storage_delta(self) -> int:
        """Flip-flop bits per tile this design adds over the default design (negative if it saves some)."""
        return sum(CATALOG[n].storage_bits(v) - CATALOG[n].storage_bits(CATALOG[n].default) for n, v in self.values)

    def generated_checks(self) -> list[tuple[str, str]]:
        """``(generated file, text)`` pairs a build of this design must contain."""
        checks = []
        for name, value in self.changed().items():
            for edit in CATALOG[name].edits:
                if edit.generated:
                    checks.append((edit.generated, edit.text(value)))
        return checks


def parse_design(text: str, space: "RtlSpace") -> RtlDesign | None:
    """A design from :meth:`RtlDesign.describe` text, or ``None`` if it is
    malformed or names a value outside *space*. Missing parameters take their defaults."""
    choices: dict = {}
    for token in text.split():
        name, sep, raw = token.partition("=")
        if not sep or name not in space.values or name in choices:
            return None
        param = CATALOG[name]
        if isinstance(param.default, int):
            if not raw.lstrip("-").isdigit():
                return None
            choices[name] = int(raw)
        else:
            choices[name] = raw.strip().upper()
    design = space.design(choices)
    return design if space.contains(design) else None


@dataclass(frozen=True)
class RtlSpace:
    """The designs a search may choose: for each searched parameter, the
    values it may take. A parameter left out keeps its default."""

    values: dict[str, tuple] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("an RTL space needs at least one parameter")
        for name, allowed in self.values.items():
            if name not in CATALOG:
                raise ValueError(f"unknown RTL parameter {name!r}; valid: {sorted(CATALOG)}")
            param = CATALOG[name]
            if not allowed or any(v not in param.values for v in allowed):
                raise ValueError(f"{name} values must come from {param.values}, got {allowed!r}")
            if param.default not in allowed:
                raise ValueError(f"{name} values must include the default {param.default!r}")

    @property
    def knobs(self) -> dict[str, tuple]:
        """Every choice a design makes, one per searched parameter."""
        return {name: tuple(self.values[name]) for name in sorted(self.values)}

    def design(self, choices: dict) -> RtlDesign:
        return RtlDesign.of({n: v for n, v in choices.items() if n in self.values}, tuple(self.values))

    def default(self) -> RtlDesign:
        return self.design({})

    def contains(self, design: RtlDesign) -> bool:
        return {n for n, _ in design.values} == set(self.values) and all(
            v in self.values[n] for n, v in design.values
        )

    def count(self) -> int:
        total = 1
        for allowed in self.values.values():
            total *= len(allowed)
        return total

    def sample(self, rng: random.Random) -> RtlDesign:
        return self.design({name: rng.choice(allowed) for name, allowed in self.knobs.items()})

    def grid(self, points: dict[str, tuple]) -> list[RtlDesign]:
        """Every combination of *points*, with the other parameters at their defaults."""
        unknown = set(points) - set(self.values)
        if unknown:
            raise ValueError(f"grid names parameters outside the space: {sorted(unknown)}")
        names = list(points)
        return [self.design(dict(zip(names, combo))) for combo in itertools.product(*(points[n] for n in names))]

    def one_at_a_time(self) -> list[RtlDesign]:
        """The default design, then each parameter alone at each of its other values."""
        designs = [self.default()]
        for name, allowed in self.knobs.items():
            designs += [self.design({name: v}) for v in allowed if v != CATALOG[name].default]
        return designs

    def describe(self) -> str:
        """The space in words, for the LLM proposer's prompt."""
        lines = []
        for name, allowed in self.knobs.items():
            param = CATALOG[name]
            cost = f"; {param.bits_per_unit} bits per tile per unit of the value" if param.bits_per_unit else "; no storage"
            lines.append(
                f"- {name} (default {param.default}; one of {', '.join(str(v) for v in allowed)}{cost}): "
                f"{param.description}"
            )
        return "\n".join(lines)


def edits_for(design: RtlDesign, read: Callable[[str], str]) -> dict[str, str]:
    """The files *design* changes, as ``{path: new full text}``.

    *read* returns a file's current text. A parameter at its default changes
    nothing, so the default design gives ``{}``.

    Raises:
        ValueError: an edit's old text is not in its file exactly once.
    """
    texts: dict[str, str] = {}
    for name, value in design.changed().items():
        param = CATALOG[name]
        for edit in param.edits:
            text = texts[edit.path] if edit.path in texts else read(edit.path)
            old, new = edit.text(param.default), edit.text(value)
            found = text.count(old)
            if found != 1:
                raise ValueError(f"{name}: {edit.path} holds {old!r} {found} times, not once")
            texts[edit.path] = text.replace(old, new)
    return texts


def read_in(root: str) -> Callable[[str], str]:
    """A reader of files under the checkout at *root* that keeps their line endings."""

    def read(path: str) -> str:
        with open(Path(root) / path, encoding="utf-8", newline="") as f:
            return f.read()

    return read
