% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Results

The table gives each approach's Verilator verdict and end-to-end wall time. [Methodology](methodology.md) describes the setup.

| Core | Mesh | (a) Manual | (b) One-shot | (c) MACE loop |
|---|---|---|---|---|
| Ariane | 2x2 | pass, 33.9 s | fail, 10.4 s * | pass, 750.1 s |
| Ariane | 4x4 | pass, 271.9 s | fail, 3820.9 s | pass, 250.9 s |
| PicoRV32 | 2x2 | pass, 9.8 s | fail, 12.0 s * | pass, 70.7 s |
| PicoRV32 | 4x4 | pass, 53.9 s | fail, 13.4 s * | pass, 100.6 s |

\* Invalid configuration (L1.5 size 0), rejected before any build.

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

A stub that fails once and then passes exercises the detect, diagnose, and replan cycle; none of the passing runs above needed it. In live runs the cycle failed once, on a PicoRV32 4x4 build cached before patch fix 10. The build ID covers only the configuration, so all three iterations reused the stale build, and triage never identified it. A clean rebuild passed.
