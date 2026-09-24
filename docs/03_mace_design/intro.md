% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Introduction

This document describes how MACE turns a hardware objective into a verified OpenPiton design.
It covers the loop in `mace/`; the CHIA adapter underneath it has its own part, [chia_openpiton Adapter](../04_chia_openpiton/intro.md).
The [MACE Requirements Specification](../02_mace_requirements/mace_requirements_specification.md) states what the loop does, and this part explains how.

## Design Goals

- **Verilator decides.** A `config` or `workload` task passes only on the verdict `pass`, and a `unit_test` task only on a successful Verilator build. A simulation's exit code cannot make a task pass, and the agent's report does not count.
- **The oracle stays fixed.** The C programs in `mace/workloads/` are checksummed, and a run stops before its first LLM call if they no longer match.
- **One checkout per concurrent task.** Parallel tasks never share a checkout (see [Parallel Task Execution](task_execution.md)).
- **Plain-text decisions.** Agents answer in prose that carries directive lines such as `TASK:`. The parsers drop malformed lines, and the loop decides what to do when a directive is missing.
- **Read-only diagnosis.** The diagnostic tools only read logs and binaries. A fix becomes a task in the next plan.
- **Bounded runs.** Iterations, dollars, and wall-clock time are independent limits.
- **Recorded runs.** Every run, iteration, task, and failure goes into SQLite, and remote calls carry replay tags.
- **Frozen inputs.** `MaceSpec`, `Task`, and `PitonConfig` are frozen dataclasses, so all tasks in a run see one specification.

## Terms

- **objective**: The English statement of what a run should achieve (`MaceSpec.objective`). The planner turns it into tasks. `MaceSpec.target_mesh` sets the mesh size of every build.
- **MaceSpec**: The input to one run (`mace/spec.py`): `workloads`, `objective`, `core` (`ariane`, `sparc`, or `pico`; default `ariane`), `target_mesh` (default `(1, 1)`, 1 to 256 tiles per axis), `budget`, and `coverage` (default `False`).
- **task**: One node of the planner's DAG (`mace.spec.Task`), with an `id`, `deps`, a `kind` (`config`, `workload`, or `unit_test`), an instruction (`spec`), and optional `caches` and `config_rtl` overrides.
- **level**: A group of tasks whose dependencies all lie in earlier groups. Tasks in one level can run in parallel.
- **gate workload**: A program in `MaceSpec.workloads`. The loop simulates the first one to decide whether a `config` or `workload` task passes. It can be a C program in `mace/workloads/` or an OpenPiton test such as `addi.S`.
- **verdict**: The outcome parsed from a run's `sim.log`: `pass`, `fail`, `timeout`, `maxcycles`, or `None` when no verdict line appears.
- **iteration**: One cycle of planning, execution, and recording, plus failure analysis when a task fails.
- **build ID**: `mace_` plus the first 12 hex digits of `PitonConfig.key`, a SHA-256 hash of the configuration. Each build ID has its own model directory under `$PITON_ROOT/build/`.
- **run ID**: A 12-digit hex identifier that keys every database row of a run.

## Organization

[Architecture](architecture.md) maps the pipeline to modules.
[Planner](planner.md), [Parallel Task Execution](task_execution.md), [Integration and Verification](integrator.md), and [Failure Analysis](failure_analysis.md) follow the stages in order.
[Budgets and Replay](budget_and_replay.md) covers stop conditions, the checksum guard, run statuses, and replay.
