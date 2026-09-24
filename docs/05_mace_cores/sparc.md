% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# OpenSPARC T1

OpenSPARC T1 is OpenPiton's original tile core, reached through its CCX transducer. MACE selects it with `core="sparc"`. OpenPiton has no C diagnostic environment for it, so its workloads are SPARC assembly.

## Status

T1 builds under Verilator 5.020 but does not run, so it is left out of the evaluation.

The core never receives the I/O bridge's power-on wake-up interrupt. The bridge model (`ciop_iob.v.pyv`) sends it, but `cmp_pcxandcpx.v` reports no received interrupt vector. That file marks a T1 thread active only when its reset interrupt (`INT_RET`, bits [17:16] = 01) arrives. No thread becomes active, and the run ends with the `maxcycles` verdict.

OpenPiton's own Verilator CI recipe for T1 (`.gitlab-ci.yml`) stalls the same way here. That rules out MACE's `MINIMAL_MONITORING` and cache flags as the cause. OpenPiton's CI uses Verilator 4. Verilator 5 refuses this design without `--timing` or `--no-timing`. With `--timing`, the run aborts at startup, because the testbench (`piton/tools/verilator/my_top.cpp`) advances time by hand. The likeliest cause is Verilator 5's scheduler exposing an ordering race on the interrupt path. Confirming it needs a Verilator 4 build.

## Workload compatibility

`barrier_atomic.c` cannot run on T1 regardless of the stall. Its `atomic_read()` helper uses `util.h`'s `ATOMIC_FETCH_OP` macro, which expands to RISC-V inline assembly.
