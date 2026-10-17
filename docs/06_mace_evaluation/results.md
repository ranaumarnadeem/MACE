% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Results

The table gives each approach's Verilator verdict and end-to-end wall time. [Methodology](methodology.md) describes the setup.

Every run on this page, the table's, the coverage run, and the seeded-failure runs below, is from the 0.0.1 pre-release (commit `05dd97e`, 2026-09-25), with patch fixes 1 to 12; the table has one run per cell. Those runs made a per-task LLM call and ended a run on the planner's first unusable reply. The loop has changed since: [Fixed Checks](../03_mace_design/fixed_checks.md) describes the checks added after them, which ask the planner again for a reply with no plan and turn per-task calls off. A larger evaluation on the current code, with the baselines and ablations of the [Evaluation Harness](harness.md), is running in October 2026, and this page will report it when it finishes.

| Core | Mesh | (a) Manual | (b) One-shot | (c) MACE loop |
|---|---|---|---|---|
| Ariane | 2x2 | pass, 33.9 s | fail, 10.4 s * | pass, 750.1 s |
| Ariane | 4x4 | pass, 271.9 s | fail, 3820.9 s | pass, 250.9 s |
| PicoRV32 | 2x2 | pass, 9.8 s | fail, 12.0 s * | pass, 70.7 s |
| PicoRV32 | 4x4 | pass, 53.9 s | fail, 13.4 s * | pass, 100.6 s |

\* Invalid configuration (L1.5 size 0), rejected before any build.

The logs and run databases behind this page are attached to the repository's [0.0.1 pre-release](https://github.com/ranaumarnadeem/MACE/releases/tag/v0.0.1).

## One-shot baseline

The one-shot baseline fails all four. In three, the LLM set the L1.5 size to zero, though every OpenPiton tile needs an L1.5. `PitonConfig` rejected those configurations before building, and the baseline has no retry path. Both PicoRV32 attempts included the named `CONFIG_DISABLE_BIST_CLEAR` define. On the 4x4 Ariane mesh the LLM chose oversized caches, including a 4 MB L2 per tile: the build took 35 minutes and the simulation timed out before any tile finished.

## MACE loop

The loop passes all four, and `sim.log` shows each tile reaching its good trap.

- Ariane 2x2 passed in four tasks, including an L1D-associativity variant the planner chose to verify, which needed a separate build.
- Ariane 4x4 passed in two tasks on a cached build.
- PicoRV32 passed in a single task at both sizes, with the planner requesting the BIST define through `CONFIG_RTL:`.

## What made multi-tile meshes pass

Two patch fixes let multi-tile meshes pass:

- Patch fix 11 polls the exit barrier in `syscalls.c` with atomic reads, which works around dropped cache invalidations in CVA6's L1.5 adapter. See [Ariane (CVA6)](../05_mace_cores/ariane.md).
- Patch fix 10 widens the monitor's `finish_mask` past 8 tiles.

Neither mesh needed changes to the loop's planning or verification logic.

## Coverage

A 2x2 Ariane run of `barrier_atomic.c`, on a build instrumented for line coverage, covers 9497 of 26900 lines (35.3%). All four tiles pass. See [Code Coverage](../01_mace_user/Code_Coverage.md).

## Recovery

No run in the table needed a replan. The seeded-failure runs (see [Baselines](../01_mace_user/Baselines.md)) break the loop's first plan and leave later plans alone. Each run builds a 2x2 mesh and has at most three iterations:

| Core | Objective | Change to the first plan | Runs | Recovered |
|---|---|---|---|---|
| Ariane | The table's | Adds `PITON_FPGA_SYNTH`, so the build fails with `%Error-PINNOTFOUND` | 6 | 4 |
| PicoRV32 | The table's, which names `CONFIG_DISABLE_BIST_CLEAR` | Removes that define, so the simulation times out | 6 | 5 |
| PicoRV32 | The default, which names no define | Adds `PITON_FPGA_SYNTH` | 3 | 0 |

Each recovered run passed in its first replan, in 90 to 404 s of loop time for Ariane and 96 to 251 s for PicoRV32. Of the other runs, one Ariane run ended with `planning_failed` when the planner's first reply held no usable task graph. One Ariane run and one PicoRV32 run ended in `error` when a task prompt's reply was cut off at the model's output limit.

A replan starts from the objective and the triage feedback and never sees the broken plan, so these runs exercise the cycle more than the diagnosis. Triage named `PITON_FPGA_SYNTH` in 2 of its 8 diagnoses of the seeded build failure, and the other 6 blamed the RTL behind the missing `async_mux` pin. For the timed-out PicoRV32 simulations, it twice found the core stuck at its reset vector, and it never named `CONFIG_DISABLE_BIST_CLEAR`.

With the default objective, the planner requested no RTL defines, so after the seeded build failure every simulation timed out on the missing `CONFIG_DISABLE_BIST_CLEAR`. The later diagnoses followed the `async_mux` pin, invented defines such as `CONFIG_ASYNC_RESET`, or planned unit tests at a guessed module path. A fourth run stalled on an LLM call while the host slept, and is recorded as `error`.
