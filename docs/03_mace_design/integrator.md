% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Integration and Verification

`mace/integrator.py` makes no LLM call.
It decides the order in which tasks run and whether an iteration continues.

## Dependency Order

`topological_levels()` groups the tasks into levels, keeping their relative order within a level, and raises `ValueError` on a duplicate id, an unknown dependency, or a cycle.
`integrate_parallel()` runs the levels in order and stops after the first level that holds a failed task, so no later task builds on a tree that failed verification.

The integrator applies no diffs of its own.
A task changes the design through its build configuration, which carries its `CACHES:` and `CONFIG_RTL:` overrides, and through any tools the caller gives the agent.
A `unit_test` task also scaffolds its environment and edits its testbench (see [Parallel Task Execution](task_execution.md)).
The drivers in the repository, `examples/mace_end_to_end.py` and the `mace` shell, pass no tools to `run_mace_loop()`.

## Build

`OpenPitonWorkspaceNode.build()` runs `sims <flags> -build_id=<build ID> -vlt_build`, passing `--no-timing` to Verilator 5 and later.
A build succeeds when `sims` exits 0 within 7200 s and the model binary exists.
When the marker and binary of an earlier successful build with the same build ID exist, `build()` returns that build with `reused=True` and skips `sims`.

`_config_for_task()` constructs its `PitonConfig` directly rather than through `configure()`, so the source-revision, Verilator-version, and diff fields stay empty.
A loop task's build ID therefore covers its configuration only.
A source edit leaves the build ID unchanged, and the loop reuses the earlier build.

## Run

For a `config` or `workload` task, `run()` simulates the first entry of `MaceSpec.workloads` in a new directory under the model's `runs/`.
It passes `-rtl_timeout=1000000` and an `-asm_diag_root` pointing at `mace/workloads/`, which sims searches in addition to the checkout's own diags.
The finish mask holds one `1` per tile, so a multi-tile run passes only when every tile reaches the good trap.
The planner prompt lists every gate workload; the integrator runs the first.

## Verdict

`chia_openpiton.parse.sim_verdict()` checks the whole `sim.log` against these patterns, in order:

| Transcript | Verdict |
|---|---|
| `Simulation -> FAIL(...)` with `TIMEOUT` in the message | `timeout` |
| any other `Simulation -> FAIL(...)` | `fail` |
| `Simulation -> (terminated by reaching max cycles = N)` | `maxcycles` |
| `Simulation -> PASS` | `pass` |
| `-> timeout happen` | `timeout` |
| none of the above | `None` |

A failure marker therefore wins over a `PASS` line.
A transcript with no verdict from a run that hit the 3600 s wall-clock limit counts as `timeout`.
See [Parsers](../04_chia_openpiton/parsers.md).

## Pass Criteria

`PitonRunResult.decide()` holds the rule: `returncode != -1 and verdict == "pass"`.
A return code of -1 marks a timeout or a failed launch; any other exit code is ignored, because a simulation exits 0 whether or not the program passed.
A `config` or `workload` task passes when its run succeeds, and fails with `run=None` when its build fails.
A `unit_test` task passes when its build succeeds.
An iteration passes when it produced results and all of them passed.
`record_iteration()` stores each task row with the cache geometry its build used.
