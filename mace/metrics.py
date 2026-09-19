"""mace.metrics -- the loop's own record: runs, iterations, tasks, failures.

Backed by chia.database.sqlite_node.SQLiteNode (a plain @ChiaFunction-based
node, not an actor -- every write here is a local call by default, so
tier-0 tests need no Ray; see open_db()'s ray_placement argument for the
one place that changes).

Every write is INSERT OR REPLACE keyed by the row's full primary key
(run_id[, iteration[, task_id]]), so recording the same task twice --
a retried task, a resumed run -- overwrites the same row with fresher
data rather than duplicating it.

The five metrics the proposal promises are graduated from these tables at
read time (see summary()), not stored as their own thing.
"""

from __future__ import annotations

import json
import time
import uuid

from chia.database.sqlite_node import SQLiteNode

from mace.spec import MaceSpec, PostMortem, StepResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    core TEXT NOT NULL,
    x_tiles INTEGER NOT NULL,
    y_tiles INTEGER NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS iterations (
    run_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    num_tasks INTEGER NOT NULL DEFAULT 0,
    num_passed INTEGER NOT NULL DEFAULT 0,
    wall_s REAL NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, iteration)
);

CREATE TABLE IF NOT EXISTS tasks (
    run_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    spec TEXT NOT NULL,
    passed INTEGER NOT NULL,
    build_success INTEGER NOT NULL,
    run_verdict TEXT,
    wall_s REAL NOT NULL DEFAULT 0,
    caches TEXT,
    module TEXT,
    PRIMARY KEY (run_id, iteration, task_id)
);

CREATE TABLE IF NOT EXISTS failures (
    run_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    diagnosis TEXT NOT NULL,
    fix TEXT,
    recovered INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, iteration, task_id)
);

CREATE TABLE IF NOT EXISTS post_mortems (
    run_id TEXT PRIMARY KEY,
    assessment TEXT NOT NULL,
    explanation TEXT,
    next_steps TEXT
);
"""


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def open_db(db_path: str, *, ray_placement: bool = True) -> SQLiteNode:
    """A metrics DB, schema already applied.

    ``ray_placement=True`` (the default, for real use) pins the node to
    whichever worker calls this -- one metrics DB per run, living durably
    on one machine for the run's life, matching the design plan's
    ``pin_to_current_node=True``. Tests pass ``ray_placement=False`` to get
    SQLiteNode's no-Ray-needed path (``require_colocated=False``) instead.
    """
    if ray_placement:
        node = SQLiteNode(db_path, pin_to_current_node=True)
    else:
        node = SQLiteNode(db_path, require_colocated=False)
    node.init_schema(SCHEMA)
    # CREATE TABLE IF NOT EXISTS does not add columns to a tasks table that
    # already exists from before `caches` was added -- this project's own
    # real runs/*.db files predate it. Add the column the first time an
    # older db is reopened; a fresh db already has it from SCHEMA above, so
    # this is expected to no-op there.
    try:
        node.execute("ALTER TABLE tasks ADD COLUMN caches TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    # Same retrofit as caches above, for module (added when unit_test tasks
    # started recording which module they target -- see _record_task).
    try:
        node.execute("ALTER TABLE tasks ADD COLUMN module TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    return node


def start_run(db: SQLiteNode, spec: MaceSpec, run_id: str | None = None) -> str:
    """Record a new run's identity; returns the run_id (generated if omitted)."""
    run_id = run_id or new_run_id()
    db.execute(
        "INSERT OR REPLACE INTO runs "
        "(run_id, objective, core, x_tiles, y_tiles, started_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'running')",
        (
            run_id,
            spec.objective,
            spec.core,
            spec.target_mesh[0],
            spec.target_mesh[1],
            time.time(),
        ),
    )
    return run_id


def finish_run(db: SQLiteNode, run_id: str, status: str) -> None:
    db.execute(
        "UPDATE runs SET finished_at = ?, status = ? WHERE run_id = ?",
        (time.time(), status, run_id),
    )


def record_iteration(
    db: SQLiteNode,
    run_id: str,
    iteration: int,
    results: tuple[StepResult, ...],
    wall_s: float,
    usd: float = 0.0,
) -> None:
    """One row for the iteration, plus one row per task it ran."""
    num_passed = sum(1 for r in results if r.passed)
    db.execute(
        "INSERT OR REPLACE INTO iterations "
        "(run_id, iteration, num_tasks, num_passed, wall_s, usd) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, iteration, len(results), num_passed, wall_s, usd),
    )
    for result in results:
        _record_task(db, run_id, iteration, result)


def _record_task(db: SQLiteNode, run_id: str, iteration: int, result: StepResult) -> None:
    """One task row, recording the cache geometry the build *actually* used
    (``result.build.config.caches``), not what the task's own spec text
    asked for -- the two can disagree (a Planner-requested override that
    never reached the build is exactly the failure mode this column exists
    to make visible), and only the build's own resolved PitonConfig is
    ground truth for what really got tested.
    """
    wall_s = result.build.wall_time_s + (result.run.wall_time_s if result.run else 0.0)
    db.execute(
        "INSERT OR REPLACE INTO tasks "
        "(run_id, iteration, task_id, kind, spec, passed, build_success, run_verdict, wall_s, caches) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            iteration,
            result.task.id,
            result.task.kind,
            result.task.spec,
            int(result.passed),
            int(result.build.success),
            result.run.verdict if result.run else None,
            wall_s,
            json.dumps(result.build.config.caches, sort_keys=True),
        ),
    )


def record_failure(
    db: SQLiteNode,
    run_id: str,
    iteration: int,
    task_id: str,
    diagnosis: str,
    fix: str = "",
    recovered: bool = False,
) -> None:
    """One taxonomy row for a failed task (see mace.agents.KNOWN_DIAGNOSES)."""
    db.execute(
        "INSERT OR REPLACE INTO failures "
        "(run_id, iteration, task_id, diagnosis, fix, recovered) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, iteration, task_id, diagnosis, fix, int(recovered)),
    )


def mark_recovered(db: SQLiteNode, run_id: str, iteration: int, task_id: str) -> None:
    """A later attempt at the same task passed -- the diagnosis led somewhere."""
    db.execute(
        "UPDATE failures SET recovered = 1 WHERE run_id = ? AND iteration = ? AND task_id = ?",
        (run_id, iteration, task_id),
    )


def record_post_mortem(db: SQLiteNode, run_id: str, post_mortem: PostMortem) -> None:
    """The final synthesis for a run that never reached "passed" -- see
    mace.report.generate_post_mortem. One row per run, same INSERT OR
    REPLACE idempotency convention as every other table here."""
    db.execute(
        "INSERT OR REPLACE INTO post_mortems (run_id, assessment, explanation, next_steps) "
        "VALUES (?, ?, ?, ?)",
        (run_id, post_mortem.assessment, post_mortem.explanation, post_mortem.next_steps),
    )


def get_post_mortem(db: SQLiteNode, run_id: str) -> PostMortem | None:
    """The recorded post-mortem for *run_id*, or ``None`` if it never got one
    (the run passed, hit a checksum mismatch, or failed planning outright --
    see run_mace_loop's docstring for which statuses produce a post-mortem
    at all)."""
    row = db.query_one(
        "SELECT assessment, explanation, next_steps FROM post_mortems WHERE run_id = ?",
        (run_id,),
    )
    if row is None:
        return None
    return PostMortem(
        assessment=row["assessment"],
        explanation=row["explanation"] or "",
        next_steps=row["next_steps"] or "",
    )


def mark_all_recovered(db: SQLiteNode, run_id: str) -> None:
    """Every recorded failure in *run_id* is recovered.

    A replanned task DAG is not guaranteed to reuse the same task id for
    "retry the same thing" (that's entirely the Planner's call each time),
    so matching a specific later success back to a specific earlier failure
    by id is unreliable. This is the coarser, robust alternative mace.loop's
    replan loop actually uses: if the run ultimately finishes "passed" after
    one or more earlier iterations recorded a failure, every failure
    recorded so far in this run is marked recovered -- whatever was
    diagnosed and fixed evidently led to the run succeeding, even without
    tracing which specific task carried the fix forward.
    """
    db.execute("UPDATE failures SET recovered = 1 WHERE run_id = ?", (run_id,))


def summary(db: SQLiteNode, run_id: str) -> dict:
    """The five metrics the proposal promises, for one run."""
    return {
        "successful_tasks": db.query_value(
            "SELECT COUNT(*) FROM tasks WHERE run_id = ? AND passed = 1", (run_id,), default=0
        ),
        "iterations": db.query_value(
            "SELECT COUNT(*) FROM iterations WHERE run_id = ?", (run_id,), default=0
        ),
        "failures_recovered": db.query_value(
            "SELECT COUNT(*) FROM failures WHERE run_id = ? AND recovered = 1",
            (run_id,),
            default=0,
        ),
        "execution_time_s": db.query_value(
            "SELECT COALESCE(SUM(wall_s), 0) FROM iterations WHERE run_id = ?",
            (run_id,),
            default=0.0,
        ),
        "compute_usd": db.query_value(
            "SELECT COALESCE(SUM(usd), 0) FROM iterations WHERE run_id = ?",
            (run_id,),
            default=0.0,
        ),
    }
