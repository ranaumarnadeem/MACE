% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Architecture

`mace.orchestrator.run_mace_loop()` checks a run's inputs once, then repeats the stages from Planning to Iteration in each iteration.

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
| Planning | `mace/planner.py`, `mace/plan_check.py` | `plan()` asks the LLM for a task DAG; `check_plan()` rejects one that cannot work before any build (see [Fixed Checks](fixed_checks.md)) |
| Task Decomposition | `mace/agents.py`, `mace/integrator.py` | `parse_tasks()` reads the directives; `topological_levels()` orders the tasks |
| Parallel Execution | `mace/integrator.py`, `mace/fast_path.py` | `integrate_parallel()` runs each level across the checkouts; a plan of one design runs as one task |
| RTL/System Integration | `mace/loop.py`, `chia_openpiton/openpiton_workspace.py` | `_config_for_task()` sets each task's `PitonConfig`; `build()` compiles it |
| Verification | `chia_openpiton/openpiton_workspace.py`, `chia_openpiton/parse.py` | `run()` simulates each gate workload; `sim_verdict()` reads the verdict |
| Failure Analysis | `mace/narrowing.py`, `mace/triage.py`, `chia_openpiton/tools.py` | `narrow()` rebuilds the first failed task with its changes undone; `triage()` diagnoses it when that settles nothing |
| Iteration | `mace/orchestrator.py` | `run_mace_loop()` feeds each diagnosis into the next plan |

`mace/metrics.py` records runs, iterations, tasks, failures, and post-mortems in SQLite.
`mace/report.py` writes a post-mortem for a run that ends with `failed` or `budget_exceeded`.
`mace/replay.py` builds replay tags and turns on CHIA's cache and bypass.

## Control Flow

`run_mace_loop(piton_roots, spec, llm, db, tools=(), on_iteration=None, on_task_progress=None, labels=None, options=None, plan_hook=None)` records the run as `running` and checks the C programs in `mace/workloads/` against `CHECKSUMS`.
Each iteration then checks the wall-clock and USD limits, calls `plan()` with the feedback gathered so far, checks the plan (see [Fixed Checks](fixed_checks.md)), and hands the task DAG to `integrate_parallel()`.
The orchestrator opens one `OpenPitonWorkspaceNode` per checkout when the first iteration reaches execution, reuses the nodes in later iterations, and closes them after the loop.

`record_iteration()` stores each iteration's results, and the optional `on_iteration(iteration, results)` callback reports them to the caller.
When every task passed, or one task's design passed every gate workload (`gate_pass_ends_run`), the run ends with `passed`.
Otherwise the orchestrator narrows the first failed task, `triage()` diagnoses it when narrowing settles nothing, and `record_failure()` stores the diagnosis for later plans (see [Failure Analysis](failure_analysis.md) and [Fixed Checks](fixed_checks.md)).
Budget limits end the run with `budget_exceeded` (see [Budgets and Replay](budget_and_replay.md)).

`mace/loop.py` also holds `run_mace_step()`, which runs a single task without the planner.
`integrate()` in `mace/integrator.py` applies tasks serially on one checkout, and the orchestrator does not call it.

`plan_hook`, when given, replaces each iteration's planned tasks with what it returns; the seeded-failure runs use it to break the first plan.

`labels`, a `mace.metrics.RunLabels`, records which method, suite task, repeat, and seed a run was, with its environment as JSON; see [Results and Metrics](../01_mace_user/Results_and_Metrics.md).

## Loop Options

`options`, a `mace.spec.LoopOptions`, turns off one part of the loop for a baseline or an ablation.
The defaults are the full loop.

| Field | Default | Other values |
|---|---|---|
| `triage` | `"llm"` | `"raw"`: the next plan gets the failed task's raw evidence, the text triage would have read (`mace.triage.failure_evidence()`), and the failure is recorded as `raw_evidence`. `"off"`: the next plan gets nothing, and the failure is recorded as `not_triaged`. |
| `check` | `"sim"` | `"build"`: a task passes when its build succeeds, and no gate workload runs. |
| `reuse_builds` | `True` | `False`: every build passes `clean=True`, so no earlier model is reused. |
| `task_prompts` | `True` | `False`: tasks make no LLM call of their own. That call's reply changes nothing that is built. The orchestrator sets it to `False` for every plan while `fast_path` is on. |
| `post_mortem` | `True` | `False`: a run that ends without passing gets no post-mortem. |
| `rtl_edits` | `False` | `True`: the planner's prompt offers the `rtl` kind, and each `rtl` task's agent gets an `RtlEditTool`; see [Parallel Task Execution](task_execution.md). |
| `plan_check` | `True` | `False`: each plan runs as the planner wrote it. |
| `raw_evidence` | `True` | `False`: the next plan gets triage's label and fix without the build's own report. |
| `narrowing` | `True` | `False`: a failed task goes to triage without any probe. |
| `fast_path` | `True` | `False`: every plan keeps all its tasks and their LLM calls. |
| `planner_thinking` | `512` | The most thinking tokens one planner call may spend, set on the Vertex request. `None` leaves the amount to the model. |
| `gate_pass_ends_run` | `True` | `False`: a run passes only when every task of an iteration passes. By default it also passes when one task ran every gate program to a pass. |
| `carry_edits` | `True` | `False`: each iteration starts from the design as it stands. By default the RTL edits of the last task whose build succeeded carry into the next iteration. |

## Package Boundary

`mace` imports `chia_openpiton`, and `chia_openpiton` imports nothing from `mace`, so the adapter can move into CHIA as `chia/openpiton/` unchanged.
CI checks the boundary on every push.
