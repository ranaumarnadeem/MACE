% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Architecture

MACE runs the same pipeline in every iteration of a run.
`mace.orchestrator.run_mace_loop()` drives it.

```text
     Requirement             MaceSpec, verify_checksums()   -> checksum_mismatch
        |
+--> Planning                plan()                         -> planning_failed
|       |
|    Task Decomposition      parse_tasks(), topological_levels()
|       |
|    Parallel Execution      integrate_parallel()
|       |
|    RTL/System Integration  _config_for_task(), build()
|       |
|    Verification            run(), sim_verdict()           -> passed
|       |
|    Failure Analysis        triage()
|       |
+--- Iteration               feedback, budget check         -> budget_exceeded
```

An arrow on the right names the run status recorded when the run stops at that stage.

## Stages

Each stage maps to the modules below.

| Stage | Module | Role |
|---|---|---|
| Requirement | `mace/spec.py` | `MaceSpec` and `Budget` validate the run's inputs |
| Planning | `mace/planner.py` | `plan()` asks the LLM for a task DAG |
| Task Decomposition | `mace/agents.py`, `mace/integrator.py` | `parse_tasks()` reads the directives; `topological_levels()` orders the tasks |
| Parallel Execution | `mace/integrator.py` | `integrate_parallel()` runs each level across the checkouts |
| RTL/System Integration | `mace/loop.py`, `chia_openpiton/openpiton_workspace.py` | `_config_for_task()` sets each task's `PitonConfig`; `build()` compiles it |
| Verification | `chia_openpiton/openpiton_workspace.py`, `chia_openpiton/parse.py` | `run()` simulates the first gate workload; `sim_verdict()` reads the verdict |
| Failure Analysis | `mace/triage.py`, `chia_openpiton/tools.py` | `triage()` diagnoses the first failed task |
| Iteration | `mace/orchestrator.py` | `run_mace_loop()` feeds each diagnosis into the next plan |

`mace/metrics.py` records runs, iterations, tasks, failures, and post-mortems in SQLite.
`mace/report.py` writes a post-mortem for a run that ends without passing.
`mace/replay.py` builds the tags that make remote calls replayable.

## Control Flow

`run_mace_loop(piton_roots, spec, llm, db, tools=(), on_iteration=None, on_task_progress=None)` records the run as `running` and verifies the gate workloads against their checksums.
Each iteration then checks the wall-clock and USD limits, calls `plan()` with the feedback gathered so far, and hands the task DAG to `integrate_parallel()`.
The orchestrator opens one `OpenPitonWorkspaceNode` per checkout when the first iteration reaches execution, reuses the nodes in later iterations, and closes them after the loop.

`record_iteration()` stores each iteration's results, and the optional `on_iteration(iteration, results)` callback reports them to the caller.
When every task passed, the run ends with `passed`.
Otherwise `triage()` diagnoses the first failed task, `record_failure()` stores the diagnosis, and a feedback line joins the history that every later `plan()` call receives.
When `max_iterations` iterations end without a pass, the run ends with `budget_exceeded`.

`mace/loop.py` also holds `run_mace_step()`, which runs a single task without the planner.
`mace/integrator.py` also offers `integrate()`, a serial applier over one checkout.
The orchestrator uses `integrate_parallel()`.

## Package Boundary

`mace` imports `chia_openpiton`, and `chia_openpiton` imports nothing from `mace`, so the adapter can move into CHIA as `chia/openpiton/` unchanged.
CI checks the boundary on every push.
