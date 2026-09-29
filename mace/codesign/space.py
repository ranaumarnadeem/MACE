"""mace.codesign.space -- designs, and the space a search may pick them from.

A :class:`Design` sets every cache's size and associativity and the
interconnect. A :class:`DesignSpace` lists, per searched cache, the sizes
and associativities a search may choose, and the interconnects; caches it
leaves out keep their defaults. Every strategy draws from the same space,
so a comparison differs only in how each strategy chooses.
"""

from __future__ import annotations

import itertools
import json
import random
from dataclasses import dataclass, field

from chia_openpiton.state_def import DEFAULT_CACHES
from mace.spec import NETWORKS, Task

DEFAULT_NETWORK = "2dmesh_config"

# Ariane's L1.5 adapter (core/cache_subsystem/wt_l15_adapter.sv in the
# Ariane submodule) asserts that neither L1 has more ways than the L1.5. The
# assertion sits inside `ifndef VERILATOR, so a Verilator build of a design
# that breaks it still builds and runs; a search rejects such a design
# before building it (see Design.way_violations).
ARIANE_WAY_RULE = "On Ariane, neither the L1D nor the L1I may have more ways than the L1.5."


@dataclass(frozen=True)
class Design:
    """Every cache's geometry, as sorted ``(name, (size, assoc))`` pairs,
    and the interconnect."""

    caches: tuple[tuple[str, tuple[int, int]], ...]
    network: str = DEFAULT_NETWORK

    @classmethod
    def of(cls, caches: dict[str, tuple[int, int]], network: str = DEFAULT_NETWORK) -> "Design":
        """A design from a partial cache map; missing caches keep their defaults."""
        merged = {**DEFAULT_CACHES, **caches}
        return cls(caches=tuple(sorted((k, tuple(v)) for k, v in merged.items())), network=network)

    def caches_dict(self) -> dict[str, tuple[int, int]]:
        return dict(self.caches)

    def describe(self) -> str:
        """``l15=8192,4 l1d=8192,4 l1i=16384,4 l2=65536,4 network=2dmesh_config``:
        the form the LLM proposer reads and writes."""
        parts = [f"{name}={size},{assoc}" for name, (size, assoc) in self.caches]
        return " ".join(parts + [f"network={self.network}"])

    def as_json(self) -> str:
        return json.dumps({"caches": {k: list(v) for k, v in self.caches}, "network": self.network}, sort_keys=True)

    def way_violations(self, core: str) -> tuple[str, ...]:
        """How this design breaks :data:`ARIANE_WAY_RULE`; empty when it
        keeps the rule, and always empty for a core other than Ariane."""
        if core != "ariane":
            return ()
        caches = self.caches_dict()
        l15_ways = caches["l15"][1]
        return tuple(
            f"{name} has {caches[name][1]} ways, more than the L1.5's {l15_ways}"
            for name in ("l1d", "l1i")
            if caches[name][1] > l15_ways
        )

    def task(self, task_id: str) -> Task:
        """This design as a ``config`` task for the build-and-check path."""
        return Task(
            id=task_id, deps=(), kind="config", spec=self.describe(),
            caches=self.caches, network=self.network,
        )


def parse_design(text: str) -> Design | None:
    """A design from :meth:`Design.describe` text, or ``None`` if malformed."""
    caches: dict[str, tuple[int, int]] = {}
    network = DEFAULT_NETWORK
    for token in text.split():
        name, sep, value = token.partition("=")
        if not sep:
            return None
        if name == "network":
            network = value
        elif name in DEFAULT_CACHES:
            size, comma, assoc = value.partition(",")
            if not comma or not size.isdigit() or not assoc.isdigit():
                return None
            caches[name] = (int(size), int(assoc))
        else:
            return None
    if network not in NETWORKS:
        return None
    return Design.of(caches, network)


@dataclass(frozen=True)
class DesignSpace:
    """The designs a search may choose.

    ``sizes`` and ``assocs`` map each searched cache to its allowed values;
    ``networks`` lists the allowed interconnects. A cache missing from
    ``sizes`` keeps its default geometry in every design.
    """

    sizes: dict[str, tuple[int, ...]] = field(default_factory=dict)
    assocs: dict[str, tuple[int, ...]] = field(default_factory=dict)
    networks: tuple[str, ...] = (DEFAULT_NETWORK,)

    def __post_init__(self) -> None:
        if set(self.sizes) != set(self.assocs):
            raise ValueError("sizes and assocs must name the same caches")
        for name in self.sizes:
            if name not in DEFAULT_CACHES:
                raise ValueError(f"unknown cache {name!r}; valid: {sorted(DEFAULT_CACHES)}")
            for values, what in ((self.sizes[name], "sizes"), (self.assocs[name], "assocs")):
                if not values or any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in values):
                    raise ValueError(f"{name} {what} must be positive ints, got {values!r}")
        if not self.networks or any(n not in NETWORKS for n in self.networks):
            raise ValueError(f"networks must be drawn from {sorted(NETWORKS)}, got {self.networks!r}")

    @property
    def knobs(self) -> dict[str, tuple]:
        """Every choice a design makes: ``<cache>_size``, ``<cache>_assoc``, ``network``."""
        knobs: dict[str, tuple] = {}
        for name in sorted(self.sizes):
            knobs[f"{name}_size"] = self.sizes[name]
            knobs[f"{name}_assoc"] = self.assocs[name]
        knobs["network"] = self.networks
        return knobs

    def design(self, choices: dict) -> Design:
        """The design that makes *choices* (knob name to value); missing
        knobs take the default design's value."""
        caches = {}
        for name in self.sizes:
            size = choices.get(f"{name}_size", DEFAULT_CACHES[name][0])
            assoc = choices.get(f"{name}_assoc", DEFAULT_CACHES[name][1])
            caches[name] = (size, assoc)
        return Design.of(caches, choices.get("network", DEFAULT_NETWORK))

    def contains(self, design: Design) -> bool:
        geometry = design.caches_dict()
        for name, (size, assoc) in geometry.items():
            if name in self.sizes:
                if size not in self.sizes[name] or assoc not in self.assocs[name]:
                    return False
            elif (size, assoc) != DEFAULT_CACHES[name]:
                return False
        return design.network in self.networks

    def count(self) -> int:
        total = len(self.networks)
        for name in self.sizes:
            total *= len(self.sizes[name]) * len(self.assocs[name])
        return total

    def sample(self, rng: random.Random) -> Design:
        return self.design({knob: rng.choice(values) for knob, values in self.knobs.items()})

    def grid(self, points: dict[str, tuple]) -> list[Design]:
        """Every combination of *points* (knob name to values), in order,
        with the other knobs at their defaults."""
        unknown = set(points) - set(self.knobs)
        if unknown:
            raise ValueError(f"grid names knobs outside the space: {sorted(unknown)}")
        names = list(points)
        return [self.design(dict(zip(names, combo))) for combo in itertools.product(*(points[n] for n in names))]

    def describe(self) -> str:
        """The space in words, for the LLM proposer's prompt."""
        lines = []
        for name in sorted(self.sizes):
            sizes = ", ".join(str(s) for s in self.sizes[name])
            assocs = ", ".join(str(a) for a in self.assocs[name])
            lines.append(f"- {name}: size in bytes one of {sizes}; associativity one of {assocs}")
        fixed = [f"{n}={s},{a}" for n, (s, a) in sorted(DEFAULT_CACHES.items()) if n not in self.sizes]
        if fixed:
            lines.append(f"- fixed at their defaults: {' '.join(fixed)}")
        lines.append(f"- network one of {', '.join(self.networks)}")
        return "\n".join(lines)
