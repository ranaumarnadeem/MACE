% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Results

Verilator verdict and end-to-end wall time for each approach. See [Methodology](methodology.md) for how the runs were set up.

| Core | Mesh | (a) Manual | (b) One-shot | (c) MACE loop |
|---|---|---|---|---|
| Ariane | 2x2 | pass, 33.9 s | fail, 10.4 s * | pass, 750.1 s |
| Ariane | 4x4 | pass, 271.9 s | fail, 3820.9 s | pass, 250.9 s |
| PicoRV32 | 2x2 | pass, 9.8 s | fail, 12.0 s * | pass, 70.7 s |
| PicoRV32 | 4x4 | pass, 53.9 s | fail, 13.4 s * | pass, 100.6 s |

\* Invalid configuration (L1.5 size 0), rejected before any build.

## One-shot baseline

The one-shot baseline fails all four configurations. In three of them it set the L1.5 size to zero, which every OpenPiton tile needs, and `PitonConfig` rejected the configuration before any build. It has no retry path. On PicoRV32 it included the named `CONFIG_DISABLE_BIST_CLEAR` define both times. On the 4x4 Ariane mesh it chose oversized caches, including a 4 MB L2 per tile: the build took 35 minutes and the simulation timed out with no tile finished.

## MACE loop

The loop passes all four configurations, with every tile confirmed in `sim.log`.

- Ariane 2x2 passed in four tasks, including an L1D-associativity variant the planner chose to verify, which needed its own build.
- Ariane 4x4 passed in two tasks on cached builds.
- PicoRV32 passed in a single task at both sizes, with the planner requesting the BIST define through `CONFIG_RTL:`.

## What made multi-tile meshes pass

Manual scaling first stalled at 2x2 and never reached 4x4. Two issues caused it:

- Under Verilator 5, CVA6's L1.5 adapter drops cache invalidations ([verilator#5829](https://github.com/verilator/verilator/issues/5829)), so the plain-load exit barrier in `syscalls.c` never sees other tiles' updates. Patch fix 11 polls the barrier with atomic reads instead. See [Ariane (CVA6)](../05_mace_cores/ariane.md).
- The monitor's 32-bit `finish_mask` capped verification at 8 tiles. Patch fix 10 widens it.

The loop needed no changes to its planning or verification logic to pass either mesh.

## Coverage

A line-coverage build of the 2x2 Ariane mesh running `barrier_atomic.c` reaches 35.00% line coverage (9497 of 26900 lines), with all four tiles passing. See [Code Coverage](../01_mace_user/Code_Coverage.md).

## Recovery

The detect, diagnose, and replan cycle has been exercised at the mechanism level, with a stub that fails once and then passes. None of the passing runs above needed it. Its one live failure was a PicoRV32 4x4 build cached before the `finish_mask` fix: a build ID covers configuration but not source edits, so all three replans reused the stale build while triage blamed the RTL. A clean rebuild passed.
