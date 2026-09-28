% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Results and Metrics

`mace.metrics` records every loop run in a SQLite database.
`examples/mace_end_to_end.py` writes to `runs/mace_end_to_end.db`, and `mace shell` to `runs/mace_cli.db`; both accept `--db-path`.
The one-shot baseline prints its result and records nothing.
Git ignores `runs/` and `*.db`.

## Tables

Every insert uses `INSERT OR REPLACE` on the table's primary key, so recording the same row again overwrites it.

| Table | Primary key | Other columns |
|---|---|---|
| `runs` | `run_id` | `objective`, `core`, `x_tiles`, `y_tiles`, `started_at`, `finished_at`, `status`, `method`, `task`, `repeat`, `seed`, `meta` |
| `iterations` | `run_id`, `iteration` | `num_tasks`, `num_passed`, `wall_s`, `usd` |
| `tasks` | `run_id`, `iteration`, `task_id` | `kind`, `spec`, `passed`, `build_success`, `run_verdict`, `wall_s`, `caches`, `module`, `build_s`, `run_s`, `programs` |
| `failures` | `run_id`, `iteration`, `task_id` | `diagnosis`, `fix`, `recovered` |
| `post_mortems` | `run_id` | `assessment`, `explanation`, `next_steps` |
| `llm_calls` | `run_id`, `seq` | `iteration`, `phase`, `input_tokens`, `output_tokens`, `thinking_tokens`, `usd`, `wall_s`, `ok` |

- `run_id` is 12 hex digits.
- `runs.method` names what ran: `mace` for a plain loop run, or the baseline or ablation an evaluation run was. `runs.task`, `runs.repeat`, and `runs.seed` identify the evaluation task and repeat, and `runs.meta` holds the run's environment as JSON. A database written before these columns existed gets them empty.
- `iterations.wall_s` runs from the start of the planner call to the end of the iteration's last task, and excludes triage. `iterations.usd` is the cost of the iteration's planner, task, and triage calls; see [LLM Backends](LLM_Backends.md).
- `tasks.build_s` and `tasks.run_s` are the build time and the summed simulation time, and `tasks.wall_s` is their sum; a reused build counts as 0. `tasks.programs` lists each gate workload run with its verdict, as JSON. `tasks.caches` holds the build configuration's cache map as JSON. `tasks.module` names the module of a `unit_test` task.
- `llm_calls` holds one row per LLM call, numbered in the order the calls finished. `phase` is `plan`, `task`, `triage`, or `post_mortem`, and a post-mortem call has no iteration. `ok` is 0 for a call that failed; such a call records no tokens.
- `failures.diagnosis` is free text. The triage prompt suggests `test_bug`, `config_error`, `timeout`, `maxcycles`, `rtl_suspect`, and `testbench_mismatch`. The loop records `unknown` when the reply has no `DIAGNOSIS:` line. When a run passes after earlier failures, all of its failures are marked `recovered`.
- The post-mortem prompt asks for the assessment `fixable_config`, `likely_hardware_limitation`, or `inconclusive`.

## Run statuses

| Status | Meaning |
|---|---|
| `running` | The run started and has not finished. A killed process leaves its run in this state. |
| `passed` | Every task of one iteration passed. |
| `budget_exceeded` | `max_iterations`, `max_wall_s`, or `max_usd` ran out before any iteration passed. |
| `planning_failed` | The planner's reply held no valid task DAG. |
| `failed` | An iteration produced no task results. |
| `checksum_mismatch` | A C program in `mace/workloads/` failed its checksum; nothing ran. |
| `error` | An exception stopped the run. |

A run that ends `failed` or `budget_exceeded` after at least one iteration gets a post-mortem, unless the LLM's reply cannot be parsed.

## The five metrics

`mace.metrics.summary(db, run_id)` returns:

| Key | Computed as |
|---|---|
| `successful_tasks` | Count of `tasks` rows with `passed = 1`. |
| `iterations` | Count of `iterations` rows. |
| `failures_recovered` | Count of `failures` rows with `recovered = 1`. |
| `execution_time_s` | Sum of `iterations.wall_s`. |
| `compute_usd` | Sum of `llm_calls.usd`, which includes the post-mortem; for a run recorded before `llm_calls` existed, the sum of `iterations.usd`. |

```python
from mace.metrics import DBReader, all_runs

db = DBReader("runs/mace_end_to_end.db")
for run in all_runs(db):
    print(run["run_id"], run["status"], run["successful_tasks"], run["execution_time_s"])
```

`DBReader` reads the database with Python's `sqlite3` and starts no Ray. It adds the `caches` and `module` columns that an older database lacks, then refuses writes.
The loop writes through `open_db(path)`, a CHIA `SQLiteNode`; the first call on one starts Ray, because CHIA's profiler looks up its collector actor.
`all_runs(db)` returns every run, newest first, with its summary merged in.
`trace_run(db, run_id)` gives one run's iterations with their tasks and failures.
`failure_taxonomy(db, run_id)` counts failures by diagnosis.
`module_status(db, run_id)` gives the latest status of each `unit_test` module.
`get_post_mortem(db, run_id)` returns the post-mortem.

## mace results

`mace results` reads the database through `DBReader`, so it needs no shell session and starts no Ray. It exits with an error when the file does not exist.
Its `--db-path` default is `runs/mace_cli.db`, so name the loop script's database explicitly:

```bash
mace results --db-path runs/mace_end_to_end.db
mace results --db-path runs/mace_end_to_end.db --run-id <run_id>
mace results --db-path runs/mace_end_to_end.db --run-id <run_id> --trace
```

With no other option, it prints one row per run (`run_id`, `core`, `mesh`, `status`, `tasks`, `iterations`, `wall_s`, `usd`) and a line with the pass count, total execution time, and total cost.
The `tasks` column counts passed tasks.
`--run-id` prints that run's failure taxonomy.
`--trace` with `--run-id` prints the run as a tree: one branch per iteration, the tasks its plan dispatched with their build result and verdict, and a TRIAGE branch when a failure was diagnosed.
`--trace` without `--run-id` is an error.

## SQL

You can also query the tables directly:

```sql
SELECT iteration, task_id, kind, build_success, run_verdict, wall_s
FROM tasks
WHERE run_id = '<run_id>'
ORDER BY iteration, rowid;
```
