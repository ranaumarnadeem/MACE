% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# OpenSPARC T1

OpenSPARC T1 is OpenPiton's original tile core, reached through its CCX transducer. MACE selects it with `core="sparc"`. OpenPiton has no C diagnostic environment for it, so its workloads are SPARC assembly.

## Status

T1 runs under Verilator 5.052 and stalls under Verilator 5.020. On a 1x1 mesh with every patch fix, OpenPiton's `princeton-test-test.s` passes under 5.052, selected with `VERILATOR_ROOT` (see [OpenPitonWorkspaceNode](../04_chia_openpiton/workspace_node.md)). The same build under the Debian 5.020 that OpenPiton's environment finds by default ends with the `maxcycles` verdict. The evaluation runs under 5.020, so it leaves T1 out.

Under 5.020, the core never receives the I/O bridge's power-on wake-up interrupt. The bridge model (`ciop_iob.v.pyv`) sends it, but `cmp_pcxandcpx.v` reports no received interrupt vector. That file marks a T1 thread active only when its reset interrupt (`INT_RET`, bits [17:16] = 01) arrives. No thread becomes active, and the run ends at max cycles.

OpenPiton's own Verilator CI recipe for T1 (`.gitlab-ci.yml`) stalls the same way under 5.020, which rules out MACE's `MINIMAL_MONITORING` and cache flags as the cause. Verilator 5 refuses this design without `--timing` or `--no-timing`. With `--timing`, the run aborts at startup, because the testbench (`piton/tools/verilator/my_top.cpp`) advances time by hand. OpenPiton's CI uses Verilator 4.014, which no longer builds with Bison 3.8: the parser Bison generates includes `verilog.h`, which Verilator 4.014's build never creates.

## Workload compatibility

`barrier_atomic.c` cannot run on T1 regardless of the stall. Its `atomic_read()` helper uses `util.h`'s `ATOMIC_FETCH_OP` macro, which expands to RISC-V inline assembly.
