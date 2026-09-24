% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# OpenSPARC T1

OpenSPARC T1 is OpenPiton's original tile core, reached through its CCX transducer. MACE selects it with `core="sparc"`. Its diagnostics are SPARC assembly; OpenPiton has no C diagnostic environment for it.

## Status

T1 builds under the Verilator 5 toolchain used here but does not run, so it is left out of the evaluation.

The core never receives the I/O bridge's power-on wake-up interrupt. The bridge model (`ciop_iob.v.pyv`) sends it, but `cmp_pcxandcpx.v` never reports a received interrupt vector, so no thread becomes active and the run ends at `max_cycle` with no verdict. `cmp_pcxandcpx.v` marks a T1 thread active only when its reset interrupt (`INT_RET`, bits [17:16] = 01) arrives, so the monitor waits correctly; the interrupt itself never lands.

OpenPiton's own Verilator CI recipe for T1 (`.gitlab-ci.yml`) stalls the same way here:

```bash
sims -sys=manycore -vlt_build -x_tiles=1 -y_tiles=1
sims -sys=manycore -vlt_run -x_tiles=1 -y_tiles=1 princeton-test-test.s
```

That rules out MACE's `MINIMAL_MONITORING` and cache flags as the cause. OpenPiton's CI uses Verilator 4. Verilator 5 refuses this design without `--timing` or `--no-timing`, and `--timing` aborts at startup because the testbench (`piton/tools/verilator/my_top.cpp`) advances time by hand. The likeliest cause is Verilator 5's scheduler exposing an ordering race on the interrupt path. Confirming it needs a Verilator 4 build.

## Workload compatibility

`barrier_atomic.c` cannot run on T1 regardless of the stall. Its `atomic_read()` helper uses `util.h`'s `ATOMIC_FETCH_OP` macro, which expands to RISC-V inline assembly. T1 workloads have to be written in SPARC assembly.
