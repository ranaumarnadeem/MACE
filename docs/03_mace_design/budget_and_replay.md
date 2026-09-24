% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Budgets and Replay

## Budget

`mace.spec.Budget` holds three independent limits:

| Field | Default | Limits |
|---|---|---|
| `max_iterations` | `10` | Iterations per run |
| `max_usd` | `20.0` | Accumulated LLM cost in US dollars |
| `max_wall_s` | `3600` | Wall-clock seconds since the checksum check |

Each limit must be positive, and `max_iterations` and `max_wall_s` must be integers.
Before each iteration, `run_mace_loop()` compares elapsed time and accumulated cost with their limits and ends the run with `budget_exceeded` when either is exceeded.
An iteration already in flight runs to completion.
A run whose `max_iterations` iterations all end without a pass also ends with `budget_exceeded`.

The cost total sums `mace.llm.extract_cost_usd()` over each iteration's task-execution calls; planner, triage, and post-mortem calls do not count.
The function reads the `usage` field of each `QueryResult`: OpenCode and Antigravity results carry it, while Claude and Vertex results do not and count as `0.0`.

## Gate-Workload Checksums

`mace/workloads/CHECKSUMS` holds the SHA-256 digest of each C program in `mace/workloads/`, in `sha256sum` format.
`run_mace_loop()` calls `verify_checksums()` right after it records the run, before any LLM call or checkout access.
A changed file, a file missing from `CHECKSUMS`, or a listed file that no longer exists raises `ValueError`, and the run ends with `checksum_mismatch` and no iterations.

## Run Status

`start_run()` records each run in the `runs` table with status `running`, and `finish_run()` sets the final value:

| Status | Meaning |
|---|---|
| `passed` | Every task in an iteration passed |
| `failed` | An iteration returned no task results |
| `planning_failed` | `plan()` raised `PlanningError` |
| `budget_exceeded` | A limit ran out before any iteration passed |
| `checksum_mismatch` | The gate workloads do not match `CHECKSUMS` |
| `error` | An exception escaped the loop; it is recorded, then re-raised |

## Deterministic Replay

`integrate_parallel()` tags each remote call of a `config` or `workload` task with `mace.replay.tag_for()`:

```python
f"{run_id}/iter{iteration}/{task_id}/{phase}"  # phase: "prompt", "build", or "run"
```

`run_mace_loop()` passes its run ID and iteration number, so the remote calls of every run are tagged.
A later call with the same run ID, iteration, task id, and phase resolves to the entry the original call wrote.
`run_mace_loop()` draws a new run ID for each run, so running the loop again writes new entries.

Tags take effect once a caller enables CHIA's cache and bypass.
`enable_caching(cache_dir_path, size=8, units="GB", yaml_path=None)` starts CHIA's cache actor, which stores the return value of every tagged call to a function the YAML file marks `cache: true`.
`enable_replay(yaml_path, func_names)` registers `cache_provider` for the named functions; their calls that the YAML file marks `bypass: true` are then served from the cache without running.
A bypassed call with no cache entry fails with a `KeyError` naming the missing tag.
The tier-1 test `mace/test/cluster/replay_e2e_test.py` checks this round trip on a toy CHIA function.
The drivers in the repository call neither function.

Replay reproduces decisions: the LLM's `QueryResult` and the build and run artifacts that fix each task's pass or fail.
CHIA's cache holds return values only, so replay does not re-apply edits an agent made through its tools; git records a checkout's source state.
Planner, triage, and post-mortem calls and `unit_test` tasks run locally and carry no tag.
