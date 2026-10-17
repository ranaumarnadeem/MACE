% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Introduction

MACE (Multicore Agentic Co-Design Engine) plans, builds, and verifies OpenPiton designs on the CHIA framework.
You give it a hardware objective in English, a core, and a target mesh.
An LLM planner turns the objective into a task DAG, and Ray dispatches independent tasks in parallel, one per OpenPiton checkout.
A configuration or workload task passes only when its Verilator build succeeds and a simulation of the gate workload reports the verdict `pass`.
Fixed checks reject a plan that cannot work before any build, and find which change broke a failed task by rebuilding it.
When a task fails for another reason, a failure-analysis agent diagnoses its logs, and the planner replans with that diagnosis.
The loop stops when one task's design passes every gate workload, the budget runs out, or the planner still returns no valid DAG after being asked again twice.

## Who this manual is for

This manual is for engineers who install MACE, run the loop and its baselines, and read the results.
It assumes you can work in a Linux shell and in Python and know the basics of RTL simulation.
You need no prior knowledge of CHIA or OpenPiton.
The [MACE Design Document](../03_mace_design/intro.md) describes the loop's internals, and the [chia_openpiton Adapter](../04_chia_openpiton/intro.md) part describes the adapter.

## How the parts fit together

| Part | Location | Role |
|---|---|---|
| Adapter | `chia_openpiton/` | CHIA nodes that drive OpenPiton's `sims` tool to configure, build, run, and collect designs. It imports nothing from `mace`. |
| Loop | `mace/` | Planner, parallel task execution, verification gate, failure analysis, budgets, gate workloads, and the SQLite run database. |
| CLI | `mace/cli/` | The `mace` command: credential setup, an interactive shell, a results reader, and cluster wrappers. |
| Drivers | `examples/` | Driver scripts: the end-to-end loop, the one-shot LLM baseline, the generic-tile integration run (`generic_integrate.py`), the evaluation batch runner (`eval_batch.py`), and single-run adapter examples. |
| Patch script | `scripts/patch_openpiton.sh` | Prepares an OpenPiton checkout to build and run in this flow. |

Every build and simulation goes through the adapter.
The loop records each run, iteration, task, and failure in SQLite.
The shell and `examples/mace_end_to_end.py` are two front ends to the same loop.

## Cores

MACE supports three native OpenPiton cores: Ariane (CVA6, RV64), OpenSPARC T1, and PicoRV32.
The loop passes 2x2 and 4x4 meshes of Ariane on `barrier_atomic.c` and of PicoRV32 on `addi.S`.
OpenSPARC T1 builds under Verilator 5.020 but does not run; it runs under Verilator 5.052.
See [Ariane](../05_mace_cores/ariane.md), [OpenSPARC T1](../05_mace_cores/sparc.md), and [PicoRV32](../05_mace_cores/pico.md).

An RV32 core with no OpenPiton adapter runs on the [Generic Tile](../05_mace_cores/generic.md), described by a short core descriptor.
Ibex and a Wishbone PicoRV32 run there with hand-written wrappers, and for a core such as SERV, MACE writes the wrapper itself.

## Baselines

[Baselines](Baselines.md) explains how to run the two simplest baselines, manual mesh scaling and a one-shot LLM prompt.
The [Evaluation Harness](../06_mace_evaluation/harness.md) runs those, a retry agent, ablations of the loop, and co-design searches, and [Results](../06_mace_evaluation/results.md) reports the numbers.

## Where to start

- [Installation](Installation.md) sets up the environment and an OpenPiton checkout.
- [Quick Start](Quick_Start.md) runs the loop on a 2x2 mesh.
- [Interactive Shell](Interactive_Shell.md) describes the `mace` command, and [Running the Loop](Running_the_Loop.md) covers the spec, the budget, planner directives, and the Python API.
- [Results and Metrics](Results_and_Metrics.md) explains the run database.
- [Troubleshooting](Troubleshooting.md) lists known failures and their fixes.
