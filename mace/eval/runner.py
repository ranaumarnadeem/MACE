"""mace.eval.runner -- run every method on every suite task, and record it.

A job is one (task, method, repeat). :func:`plan_jobs` expands a suite into
jobs and shuffles them, so a slow hour on the LLM service does not land on
one method. :func:`run_batch` runs each job that has no finished run in the
database yet, so a batch stopped midway resumes where it left off. Before
each job it empties MACE's build directories on every checkout, so every
run starts from an empty build cache; builds are reused only inside a run.

Every method records its run in the same database, labelled with
:class:`mace.metrics.RunLabels` (method, suite task, repeat) and with the
environment in ``meta``: MACE's commit, each checkout's source
fingerprint, the Verilator version, the model, and the host.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import random
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from chia.database.sqlite_node import SQLiteNode

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from mace.baselines.expert import run_expert
from mace.baselines.one_shot import run_one_shot
from mace.baselines.retry_agent import run_retry_agent
from mace.codesign.area import design_area
from mace.codesign.run import run_codesign
from mace.codesign.search import BayesianSearch, GridSearch, LLMProposer, RandomSearch
from mace.eval.faults import FAULTS, first_plan_breaker
from mace.eval.suite import SuiteTask
from mace.loop import run_gate_programs
from mace.metrics import RunLabels, record_resimulation
from mace.orchestrator import run_mace_loop
from mace.spec import LoopOptions, LoopResult, MaceSpec
from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR

logger = logging.getLogger(__name__)

METHODS: dict[str, str] = {
    "mace": "The full loop.",
    "one_shot": "B1: the planner's first plan, run once. Also the no-re-plan ablation.",
    "retry_agent": "B2: one agent, one design per attempt, raw errors, one checkout.",
    "expert": "B0: the known passing configuration, built and checked once, with no LLM.",
    "no_triage": "A1: the re-plan gets raw evidence in place of a diagnosis.",
    "one_checkout": "A2: the full loop on one checkout.",
    "build_check": "A3: a task passes on its build; accepted designs are simulated afterwards.",
    "no_reuse": "A5: every task rebuilds, even a configuration already built.",
    "codesign_mace": "C0: MACE's LLM proposer picks each round's designs.",
    "codesign_random": "C1: uniform random designs, seeded by the repeat.",
    "codesign_grid": "C2: the task's fixed grid, in order.",
    "codesign_bayes": "C3: Optuna's TPE sampler, seeded by the repeat.",
}

CODESIGN_METHODS = frozenset(m for m in METHODS if m.startswith("codesign_"))

# One method per seeded fault (see mace.eval.faults): the full loop with its
# first plan broken. Only asked-for methods run, so an unverified fault runs
# only when a batch names it.
SEEDED_PREFIX = "seeded_"
for _name, _fault in FAULTS.items():
    METHODS[f"{SEEDED_PREFIX}{_name}"] = f"RQ3: the loop with its first plan broken. {_fault.description}"

# Methods with no randomness run once per task unless asked otherwise.
ONCE_METHODS = frozenset(("expert", "codesign_grid"))


def applies(method: str, task: SuiteTask) -> bool:
    """Co-design methods run on co-design tasks, the others on bring-up
    tasks; a seeded fault runs only on the cores it lists."""
    if method.startswith(SEEDED_PREFIX):
        return task.kind == "bringup" and task.core in FAULTS[method[len(SEEDED_PREFIX):]].cores
    return (method in CODESIGN_METHODS) == (task.kind == "codesign")

# A run in one of these states counts as done; a run left "running" by a
# killed process, or ended "error" by an exception, runs again.
FINISHED = frozenset(("passed", "failed", "planning_failed", "budget_exceeded", "checksum_mismatch"))

BUILD_ID_PREFIX = "mace_"


@dataclass(frozen=True)
class Job:
    task: SuiteTask
    method: str
    repeat: int

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.task.id, self.method, self.repeat)


@dataclass
class RunEnv:
    """What every job in a batch shares."""

    piton_roots: tuple[str, ...]
    llm: object
    db: SQLiteNode
    meta: dict = field(default_factory=dict)
    task_prompts: bool = True
    clear_cache: bool = True


def plan_jobs(
    tasks: tuple[SuiteTask, ...],
    methods: tuple[str, ...],
    repeats: int,
    once_repeats: int = 1,
    seed: int = 0,
) -> list[Job]:
    """Every (task, method, repeat), shuffled with *seed*.

    Methods in :data:`ONCE_METHODS` get *once_repeats* repeats, the others
    *repeats*. A method runs only on the tasks it :func:`applies` to.
    """
    unknown = [m for m in methods if m not in METHODS]
    if unknown:
        raise ValueError(f"unknown methods {unknown}; known: {sorted(METHODS)}")
    jobs = [
        Job(task, method, repeat)
        for task in tasks
        for method in methods
        if applies(method, task)
        for repeat in range(once_repeats if method in ONCE_METHODS else repeats)
    ]
    random.Random(seed).shuffle(jobs)
    return jobs


def finished_keys(db) -> set[tuple[str, str, int]]:
    """(task, method, repeat) of every finished run in *db*."""
    rows = db.query(
        "SELECT task, method, repeat, status FROM runs WHERE task IS NOT NULL AND repeat IS NOT NULL",
        (),
    )
    return {(r["task"], r["method"], r["repeat"]) for r in rows if r["status"] in FINISHED}


def clear_build_cache(piton_root: str) -> int:
    """Remove every MACE model directory under *piton_root*; returns how many.

    Only directories named with MACE's build-id prefix go, so models built
    by hand or by other tools in the same checkout stay.
    """
    build_dir = Path(piton_root) / "build" / "manycore"
    if not build_dir.is_dir():
        return 0
    removed = 0
    for entry in build_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(BUILD_ID_PREFIX):
            shutil.rmtree(entry)
            removed += 1
    return removed


def environment_meta(piton_roots: tuple[str, ...], backend: str, model: str | None) -> dict:
    """The environment every run of a batch records.

    A probe that fails records its error in place of a value, so a batch
    never stops for want of a label.
    """
    repo = Path(__file__).resolve().parents[2]
    meta: dict = {
        "backend": backend,
        "model": model,
        "host": socket.gethostname(),
        "makeflags": os.environ.get("MAKEFLAGS", ""),
    }
    meta["mace_commit"] = _probe(lambda: _git(repo, "rev-parse", "HEAD"))
    meta["mace_dirty"] = _probe(lambda: bool(_git(repo, "status", "--porcelain", "--untracked-files=no")))
    checkouts = {}
    for root in piton_roots:
        version = _probe(lambda: OpenPitonWorkspaceNode.verilator_version_text(root))
        checkouts[root] = {
            "verilator": version.splitlines()[0] if isinstance(version, str) and version else version,
            "fingerprint": _probe(lambda: OpenPitonWorkspaceNode.source_fingerprint(root, version)),
        }
    meta["checkouts"] = checkouts
    return meta


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()


def _probe(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 -- a label must never stop a batch
        return f"error: {e}"


def run_job(job: Job, env: RunEnv):
    """Run *job* on an empty build cache, and record it."""
    roots = env.piton_roots[:1] if job.method in ("one_checkout", "retry_agent", "expert") else env.piton_roots
    labels = RunLabels(
        method=job.method,
        task=job.task.id,
        repeat=job.repeat,
        meta={**env.meta, "piton_roots": list(roots), "task_prompts": env.task_prompts},
    )
    if env.clear_cache:
        for root in env.piton_roots:
            clear_build_cache(root)
    spec = job.task.spec()
    if job.method in CODESIGN_METHODS:
        return _run_codesign_job(job, env, roots, spec, labels)
    if job.method.startswith(SEEDED_PREFIX):
        fault = FAULTS[job.method[len(SEEDED_PREFIX):]]
        labels = dataclasses.replace(labels, meta={**labels.meta, "fault": fault.name})
        return run_mace_loop(
            roots, spec, env.llm, env.db, labels=labels, options=LoopOptions(task_prompts=env.task_prompts),
            plan_hook=first_plan_breaker(fault),
        )
    options = LoopOptions(task_prompts=env.task_prompts)
    method = job.method
    if method == "mace" or method == "one_checkout":
        return run_mace_loop(roots, spec, env.llm, env.db, labels=labels, options=options)
    if method == "one_shot":
        return run_one_shot(roots, spec, env.llm, env.db, labels=labels, task_prompts=env.task_prompts)
    if method == "retry_agent":
        return run_retry_agent(roots[0], spec, env.llm, env.db, labels=labels)
    if method == "expert":
        return run_expert(roots, spec, job.task.expert_task(), env.db, labels=labels)
    if method == "no_triage":
        return run_mace_loop(
            roots, spec, env.llm, env.db, labels=labels, options=dataclasses.replace(options, triage="raw")
        )
    if method == "no_reuse":
        return run_mace_loop(
            roots, spec, env.llm, env.db, labels=labels,
            options=dataclasses.replace(options, reuse_builds=False),
        )
    if method == "build_check":
        result = run_mace_loop(
            roots, spec, env.llm, env.db, labels=labels, options=dataclasses.replace(options, check="build")
        )
        resimulate_accepted(env.db, result, spec)
        return result
    raise ValueError(f"unknown method {method!r}")


def area_budget_um2(task: SuiteTask) -> float | None:
    """The co-design task's area budget: its ratio times the default
    caches' area on the task's mesh, from the same area model the search
    scores designs with."""
    ratio = task.codesign.area_budget_ratio
    if ratio is None:
        return None
    tiles = task.mesh[0] * task.mesh[1]
    return ratio * design_area(task.codesign.default_design(), tiles).area_um2


def _run_codesign_job(job: Job, env: RunEnv, roots, spec: MaceSpec, labels: RunLabels):
    config = job.task.codesign
    budget = area_budget_um2(job.task)
    seed = job.repeat
    if job.method == "codesign_mace":
        strategy, seed = LLMProposer(env.llm, spec, config.space, budget), None
    elif job.method == "codesign_random":
        strategy = RandomSearch(config.space, seed=seed)
    elif job.method == "codesign_grid":
        strategy, seed = GridSearch(config.grid_designs()), None
    else:
        strategy = BayesianSearch(config.space, seed=seed)
    labels = dataclasses.replace(labels, seed=seed, meta={**labels.meta, "area_budget_um2": budget})
    return run_codesign(
        roots, spec, strategy, env.db, simulations=config.simulations, batch=config.batch,
        area_budget_um2=budget, labels=labels,
    )


def _run_program(root: str, config, program: str, spec: MaceSpec):
    return OpenPitonWorkspaceNode.run(
        root, config, program, asm_diag_root=str(WORKLOADS_DIR),
        rtl_timeout=spec.rtl_timeout or RECOMMENDED_RTL_TIMEOUT,
    )


def resimulate_accepted(db, result: LoopResult, spec: MaceSpec, run_program=_run_program) -> int:
    """Simulate the gate workloads on each design a build-only-check run
    accepted, and record the verdicts; returns the number of designs.

    Only a passed run's last iteration holds accepted designs. Each is
    simulated on the checkout that built it, found from its model
    directory, before the next job empties the build cache.
    """
    if result.status != "passed" or not result.iterations:
        return 0
    checked = 0
    for step in result.iterations[-1]:
        if not (step.passed and step.build.success and step.build.model_dir):
            continue
        root = str(Path(step.build.model_dir).parents[2])
        runs, _ = run_gate_programs(
            lambda program: run_program(root, step.build.config, program, spec), spec.workloads
        )
        for run in runs:
            record_resimulation(db, result.run_id, step.task.id, run)
        checked += 1
    return checked


def run_batch(jobs: list[Job], env: RunEnv, on_job=None) -> dict[str, int]:
    """Run each job in *jobs* that has no finished run yet.

    An exception in one job is logged and the batch moves on; the job's run
    is already recorded as ``error``, so a later batch retries it.
    ``on_job(job, result_or_exception)`` reports each job as it ends.
    Returns counts of ``skipped``, ``ran``, and ``errors``.
    """
    done = finished_keys(env.db)
    counts = {"skipped": 0, "ran": 0, "errors": 0}
    for job in jobs:
        if job.key in done:
            counts["skipped"] += 1
            continue
        try:
            result = run_job(job, env)
        except Exception as e:  # noqa: BLE001 -- one failed job must not end the batch
            logger.exception("job %s failed", job.key)
            counts["errors"] += 1
            if on_job is not None:
                on_job(job, e)
            continue
        counts["ran"] += 1
        if on_job is not None:
            on_job(job, result)
    return counts
