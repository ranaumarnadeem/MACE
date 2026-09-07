"""mace.spec -- MaceSpec: what one MACE loop run is for.

A spec is fixed for the life of a run: which OpenPiton core and mesh size to
target, which gate workloads decide "the design still works", the objective
text the Planner turns into a task DAG, and the budget that stops the loop.
Nothing here talks to Ray, an LLM, or OpenPiton -- validation only, so it is
testable without any of that running. The immutability matters for the same
reason chia_openpiton.state_def.PitonConfig is frozen: a spec mutated mid-run
would let a task's view of "what we're building" silently drift from another
task's view of the same run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chia_openpiton.state_def import MAX_TILES_PER_AXIS, PitonCore


@dataclass(frozen=True)
class Budget:
    """Stop conditions for one MACE loop run.

    All three are independent hard caps. A run can be cheap on one axis and
    blow another -- few iterations but a stuck worker burns wall time, a
    single iteration on an expensive model burns dollars -- so the loop must
    check all three rather than assuming one implies the others.
    """

    max_iterations: int = 10
    max_usd: float = 20.0
    max_wall_s: int = 3600

    def __post_init__(self) -> None:
        if not isinstance(self.max_iterations, int) or isinstance(self.max_iterations, bool):
            raise ValueError(f"max_iterations must be an int, got {self.max_iterations!r}")
        if self.max_iterations <= 0:
            raise ValueError(f"max_iterations must be positive, got {self.max_iterations}")
        if self.max_usd <= 0:
            raise ValueError(f"max_usd must be positive, got {self.max_usd}")
        if self.max_wall_s <= 0:
            raise ValueError(f"max_wall_s must be positive, got {self.max_wall_s}")


@dataclass(frozen=True)
class MaceSpec:
    """One MACE loop run: what to build, what "done" means, and its limits."""

    workloads: tuple[str, ...]
    objective: str
    core: PitonCore = "ariane"
    target_mesh: tuple[int, int] = (1, 1)
    budget: Budget = field(default_factory=Budget)

    def __post_init__(self) -> None:
        if self.core not in ("ariane", "sparc"):
            raise ValueError(f"core must be 'ariane' or 'sparc', got {self.core!r}")
        if not self.workloads:
            raise ValueError("workloads must be non-empty -- nothing to gate on")
        for w in self.workloads:
            if not isinstance(w, str) or not w.strip():
                raise ValueError(f"workload names must be non-empty strings, got {w!r}")
        if not self.objective.strip():
            raise ValueError("objective must be a non-empty string")
        if not (isinstance(self.target_mesh, tuple) and len(self.target_mesh) == 2):
            raise ValueError(
                f"target_mesh must be an (x_tiles, y_tiles) tuple, got {self.target_mesh!r}"
            )
        for axis, n in zip(("target_mesh.x", "target_mesh.y"), self.target_mesh):
            if not isinstance(n, int) or isinstance(n, bool):
                raise ValueError(f"{axis} must be an int, got {n!r}")
            if not 1 <= n <= MAX_TILES_PER_AXIS:
                raise ValueError(f"{axis} must be 1..{MAX_TILES_PER_AXIS}, got {n}")
        if not isinstance(self.budget, Budget):
            raise ValueError(f"budget must be a Budget, got {type(self.budget).__name__}")
