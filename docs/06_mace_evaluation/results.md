% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Results

Verilator verdicts and end-to-end wall times for each approach; [Methodology](methodology.md) describes the setup.

| Core | Mesh | (a) Manual | (b) One-shot | (c) MACE loop |
|---|---|---|---|---|
| Ariane | 2x2 | pass, 33.9 s | fail, 10.4 s * | pass, 750.1 s |
| Ariane | 4x4 | pass, 271.9 s | fail, 3820.9 s | pass, 250.9 s |
| PicoRV32 | 2x2 | pass, 9.8 s | fail, 12.0 s * | pass, 70.7 s |
| PicoRV32 | 4x4 | pass, 53.9 s | fail, 13.4 s * | pass, 100.6 s |

\* Invalid configuration (L1.5 size 0), rejected before any build.

## One-shot baseline

The one-shot baseline fails all four. In three, the LLM set the L1.5 size to zero, although every OpenPiton tile needs an L1.5, and `PitonConfig` rejected the configuration before building; the baseline has no retry path. Both PicoRV32 attempts included the named `CONFIG_DISABLE_BIST_CLEAR` define. On the 4x4 Ariane mesh the LLM chose oversized caches, including a 4 MB L2 per tile: the build took 35 minutes and the simulation timed out before any tile finished.

## MACE loop

The loop passes all four, and `sim.log` shows each tile reaching its good trap.

- Ariane 2x2 passed in four tasks, including an L1D-associativity variant the planner chose to verify, which needed a separate build.
- Ariane 4x4 passed in two tasks on cached builds.
- PicoRV32 passed in a single task at both sizes, with the planner requesting the BIST define through `CONFIG_RTL:`.

## What made multi-tile meshes pass

Manual scaling first stalled at 2x2 and never reached 4x4. Two issues caused it:

- Under Verilator 5, CVA6's L1.5 adapter drops cache invalidations ([verilator#5829](https://github.com/verilator/verilator/issues/5829)), so the plain-load exit barrier in `syscalls.c` never sees other tiles' updates. Patch fix 11 polls the barrier with atomic reads instead. See [Ariane (CVA6)](../05_mace_cores/ariane.md).
- The monitor's 32-bit `finish_mask` capped verification at 8 tiles. Patch fix 10 widens it.

Neither mesh needed changes to the loop's planning or verification logic.

## Coverage

A 2x2 Ariane build instrumented for line coverage covers 35.00% of lines (9497 of 26900) running `barrier_atomic.c`, with all four tiles passing. See [Code Coverage](../01_mace_user/Code_Coverage.md).

## Recovery

A stub that fails once and then passes exercises the detect, diagnose, and replan cycle; none of the passing runs above needed it. Its one live failure was a PicoRV32 4x4 build cached before the `finish_mask` fix. A build ID covers configuration but not source edits, so all three replans reused the stale build while triage blamed the RTL. A clean rebuild passed.
