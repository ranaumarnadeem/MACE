% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# PicoRV32

PicoRV32 is a small RV32I core with no cache of its own. It reaches the L1.5 through `pico_l15_transducer.v` and the related files that already exist in OpenPiton. MACE selects it with `core="pico"`, which adds `-pico` and `-rv32_target_triple=riscv64-unknown-elf` to the `sims` command line. The second flag lets the installed `riscv64-unknown-elf` toolchain assemble RV32 code.

## Status

MACE's loop passes 2x2 and 4x4 PicoRV32 meshes on `addi.S`, with every tile reaching `Hit Good trap`. See [Results](../06_mace_evaluation/results.md).

## Bring-up fixes

OpenPiton's CI builds PicoRV32 under Verilator but does not run it. Three fixes make it run:

| Fix | Where | Effect |
|---|---|---|
| Self-boot gate | `piton/design/chip/tile/pico/rtl/picorv32.v` (patch fix 6) | The core's internal `resetn` only rose on an L15 interrupt that a bare configuration never sends. A `booted` register now lets it start once after reset while keeping its sleep and wake path. |
| Monitor tracking | `piton/verif/env/manycore/pc_cmp.v.pyv` (patch fix 7) | The monitor's `active_thread` bit for pico waited on the same interrupt. It now turns on after reset, matching Ariane's block, so good and bad traps are detected. |
| BIST self-clear | RTL define `CONFIG_DISABLE_BIST_CLEAR` | OpenPiton's generic SRAM model (`bram_1rw_wrapper.v`) runs a power-on BIST clear that discarded pico's first memory writes, because pico boots faster than the other cores. The define turns the clear off. |

Fixes 6 and 7 come from `scripts/patch_openpiton.sh`. The BIST define is a build option, so a run has to request it:

- By hand: `PitonConfig(core="pico", config_rtl=("CONFIG_DISABLE_BIST_CLEAR", "MINIMAL_MONITORING"))`.
- Through the loop: the planner emits a `CONFIG_RTL:` line naming the define for its task. See [Planner](../03_mace_design/planner.md).

Each task builds from its own configuration, so the define has to be attached to the task that builds and runs the gate workload.

## Workloads

PicoRV32's OpenPiton integration has an assembler (`piton/tools/bin/rv32_as`) but no C compiler, and no `crt.S` or `syscalls.c` under `piton/verif/diag/assembly/include/riscv/pico/`. Only assembly workloads such as `addi.S` run on it. A C workload like `barrier_atomic.c` never compiles, and every tile idles at its reset vector on an empty memory image. The upstream `amoadd_w.S` test passes on pico, which shows its atomic memory operations work.
