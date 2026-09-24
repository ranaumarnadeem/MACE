% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Results and Metrics

`mace.metrics` records every loop run in a SQLite database.
`examples/mace_end_to_end.py` writes to `runs/mace_end_to_end.db`, and `mace shell` to `runs/mace_cli.db`; both accept `--db-path`.
The one-shot baseline prints its result and records nothing.
Git ignores `runs/` and `*.db`.

## Tables

Rows are inserted with `INSERT OR REPLACE` on the table's primary key, so recording the same row again overwrites it.

| Table | Primary key | Other columns |
|---|---|---|
| `runs` | `run_id` | `objective`, `core`, `x_tiles`, `y_tiles`, `started_at`, `finished_at`, `status` |
| `iterations` | `run_id`, `iteration` | `num_tasks`, `num_passed`, `wall_s`, `usd` |
| `tasks` | `run_id`, `iteration`, `task_id` | `kind`, `spec`, `passed`, `build_success`, `run_verdict`, `wall_s`, `caches`, `module` |
| `failures` | `run_id`, `iteration`, `task_id` | `diagnosis`, `fix`, `recovered` |
| `post_mortems` | `run_id` | `assessment`, `explanation`, `next_steps` |

- `run_id` is 12 hex digits.
- `iterations.wall_s` runs from the start of the planner call to the end of the iteration's last task, and excludes triage. `iterations.usd` is the per-task LLM cost; see [LLM Backends](LLM_Backends.md).
- `tasks.wall_s` is build plus simulation time, and a reused build counts as 0. `tasks.caches` is the cache geometry the build used, as JSON. `tasks.module` names the module of a `unit_test` task.
- `failures.diagnosis` is free text. The triage prompt suggests `test_bug`, `config_error`, `timeout`, `maxcycles`, `rtl_suspect`, and `testbench_mismatch`, and the loop records `unknown` when the reply has no `DIAGNOSIS:` line. When a run passes after earlier failures, all of its failures are marked `recovered`.
- The post-mortem prompt asks for the assessment `fixable_config`, `likely_hardware_limitation`, or `inconclusive`.

## Run statuses

| Status | Meaning |
|---|---|
| `running` | The run started and has not finished. A killed process leaves its run in this state. |
| `passed` | Every task of one iteration passed. |
| `budget_exceeded` | A budget cap was reached, or every iteration ran without a pass. |
| `planning_failed` | The planner's reply held no valid task DAG. |
| `failed` | An iteration produced no task results. |
| `checksum_mismatch` | A gate workload failed its checksum; nothing ran. |
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
| `compute_usd` | Sum of `iterations.usd`. |

```python
from mace.metrics import all_runs, open_db, summary

db = open_db("runs/mace_end_to_end.db", ray_placement=False)
for run in all_runs(db):
    print(run["run_id"], run["core"], run["status"], summary(db, run["run_id"]))
```

`ray_placement=False` opens the database without Ray.
`all_runs(db)` returns every run, newest first, with its summary merged in.
`trace_run(db, run_id)` returns one run's iterations with their tasks and failures, `failure_taxonomy(db, run_id)` counts failures by diagnosis, `module_status(db, run_id)` gives the latest status of each `unit_test` module, and `get_post_mortem(db, run_id)` returns the post-mortem.

## mace results

`mace results` reads the database without Ray or a shell session.
Its `--db-path` default is `runs/mace_cli.db`, so name the loop script's database explicitly:

```bash
mace results --db-path runs/mace_end_to_end.db
mace results --db-path runs/mace_end_to_end.db --run-id <run_id>
mace results --db-path runs/mace_end_to_end.db --run-id <run_id> --trace
```

With no other option, it prints one row per run (`run_id`, `core`, `mesh`, `status`, `tasks`, `iterations`, `wall_s`, `usd`) and a line with the pass count, total execution time, and total cost.
`--run-id` prints that run's failure taxonomy.
`--trace` with `--run-id` prints the run as a tree: one branch per iteration, the tasks its plan dispatched with their build result and verdict, and a TRIAGE branch when a failure was diagnosed.
`--trace` without `--run-id` is an error.

## SQL

The tables can also be queried directly:

```sql
SELECT iteration, task_id, kind, build_success, run_verdict, wall_s
FROM tasks
WHERE run_id = '<run_id>'
ORDER BY iteration, rowid;
```
