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

from chia.base.llm_call import QueryResult
from chia_openpiton.state_def import (
    DEFAULT_CACHES,
    MAX_TILES_PER_AXIS,
    PitonBuildArtifact,
    PitonCore,
    PitonRunResult,
)


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
        if not isinstance(self.max_usd, (int, float)) or isinstance(self.max_usd, bool):
            raise ValueError(f"max_usd must be a number, got {self.max_usd!r}")
        if self.max_usd <= 0:
            raise ValueError(f"max_usd must be positive, got {self.max_usd}")
        if not isinstance(self.max_wall_s, int) or isinstance(self.max_wall_s, bool):
            raise ValueError(f"max_wall_s must be an int, got {self.max_wall_s!r}")
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
    coverage: bool = False

    def __post_init__(self) -> None:
        if self.core not in ("ariane", "sparc", "pico"):
            raise ValueError(f"core must be 'ariane', 'sparc', or 'pico', got {self.core!r}")
        if not isinstance(self.coverage, bool):
            raise ValueError(f"coverage must be a bool, got {self.coverage!r}")
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


TASK_KINDS: frozenset[str] = frozenset(("config", "workload", "unit_test"))


@dataclass(frozen=True)
class Task:
    """One node in a Planner-produced task DAG.

    See mace.agents.parse_tasks -- this is deliberately just the parsed shape
    (id, deps, kind, spec), not a runnable unit; dispatch belongs to the loop.
    """

    id: str
    deps: tuple[str, ...]
    kind: str
    spec: str
    # A sorted tuple of (cache_name, (size, associativity)) pairs -- not a
    # dict, so Task stays hashable like every other frozen dataclass here.
    # None means "no override": the task builds with the mesh's default
    # cache geometry. See mace.agents.parse_cache_overrides for how this
    # gets populated from a Planner-produced CACHES: line.
    caches: tuple[tuple[str, tuple[int, int]], ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError(f"task id must be a non-empty string, got {self.id!r}")
        if self.kind not in TASK_KINDS:
            raise ValueError(f"kind must be one of {sorted(TASK_KINDS)}, got {self.kind!r}")
        for d in self.deps:
            if not isinstance(d, str) or not d.strip():
                raise ValueError(f"dep ids must be non-empty strings, got {d!r}")
        if self.caches is not None:
            seen_names: set[str] = set()
            for name, geom in self.caches:
                if name not in DEFAULT_CACHES:
                    raise ValueError(f"unknown cache {name!r}; valid: {sorted(DEFAULT_CACHES)}")
                if name in seen_names:
                    # dict(self.caches) would otherwise silently keep only the last
                    # entry, dropping an earlier override with no error.
                    raise ValueError(f"duplicate cache {name!r} in caches")
                seen_names.add(name)
                size, assoc = geom
                if size <= 0 or assoc <= 0:
                    raise ValueError(f"cache {name} size/associativity must be positive, got {geom}")

    @property
    def caches_dict(self) -> dict[str, tuple[int, int]] | None:
        """``caches`` as a plain dict, ready for ``PitonConfig(caches=...)``
        -- ``None`` when this task set no override."""
        return dict(self.caches) if self.caches is not None else None


@dataclass
class StepResult:
    """Outcome of one run_mace_step call: an edit attempt, gated by a build+run.

    Not frozen -- unlike MaceSpec/Task (validated inputs to a run), this is a
    result record, the same convention chia_openpiton.state_def's own result
    types (PitonBuildArtifact, PitonRunResult, ...) use.
    """

    task: Task
    query: QueryResult
    build: PitonBuildArtifact
    run: PitonRunResult | None
    passed: bool


@dataclass(frozen=True)
class Triage:
    """One failed task's diagnosis, from mace.triage.triage()."""

    diagnosis: str
    fix: str

    def __post_init__(self) -> None:
        if not isinstance(self.diagnosis, str) or not self.diagnosis.strip():
            raise ValueError(f"diagnosis must be a non-empty string, got {self.diagnosis!r}")


@dataclass(frozen=True)
class PostMortem:
    """A whole run's final diagnosis, from mace.report.generate_post_mortem()
    -- produced only when a run ends without ever reaching "passed" (see
    mace.orchestrator.run_mace_loop for exactly which statuses trigger this).

    Distinct from Triage: a Triage diagnoses one failed task to inform the
    next replan; a PostMortem synthesizes an entire exhausted run into a
    single verdict on whether the objective looks achievable at all.
    """

    assessment: str
    explanation: str = ""
    next_steps: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.assessment, str) or not self.assessment.strip():
            raise ValueError(f"assessment must be a non-empty string, got {self.assessment!r}")


@dataclass
class LoopResult:
    """Outcome of one full mace.orchestrator.run_mace_loop call: every
    iteration it took, and why it stopped."""

    run_id: str
    status: str  # "passed" | "failed" | "planning_failed" | "budget_exceeded"
    #             | "checksum_mismatch"
    iterations: tuple[tuple[StepResult, ...], ...]
    post_mortem: PostMortem | None = None
