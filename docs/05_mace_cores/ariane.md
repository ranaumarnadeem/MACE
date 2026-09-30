% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Ariane (CVA6)

Ariane is OpenPiton's RV64GC core, built from the CVA6 repository that OpenPiton carries as a submodule. It has its own write-through L1 caches and reaches the rest of the tile through CVA6's L1.5 adapter, `piton/design/chip/tile/ariane/core/cache_subsystem/wt_l15_adapter.sv`. MACE selects it with `core="ariane"`, which adds `-ariane` to the `sims` command line.

## Status

MACE's loop passes 2x2 and 4x4 Ariane meshes on `barrier_atomic.c`, with every tile reaching `Hit Good trap`. The manual baseline passes both sizes as well. See [Results](../06_mace_evaluation/results.md).

## Multi-tile fixes

Four patches in `scripts/patch_openpiton.sh` matter for multi-tile Ariane runs. Fixes 11 to 13 change Ariane files. Fix 10 changes the shared monitor, so it applies to every core. See [Environment Patches](../04_chia_openpiton/environment_patches.md) for the full list.

| Fix | File | Effect |
|---|---|---|
| 10 | `piton/verif/env/manycore/pc_cmp.v.pyv` | Widens `finish_mask` under Verilator. It was a 32-bit `integer`, which capped verification at 8 tiles (4 thread slots per tile). |
| 11 | `piton/verif/diag/assembly/include/riscv/ariane/syscalls.c` | Polls the exit barrier (`finish_sync0`/`finish_sync1`) with atomic reads instead of plain loads. |
| 12 | `piton/design/chip/tile/ariane/core/cva6.sv` | Names each tile's Verilator trace file after its hart ID instead of every tile sharing `trace_hart_00.dasm`. |
| 13 | `piton/design/chip/tile/ariane/core/cache_subsystem/wt_l15_adapter.sv` | Ports cva6#2809, so the L1.5 adapter stops dropping cache invalidations; see below. |

## The cache-invalidation issue

Under Verilator 5, CVA6's L1.5 adapter sometimes drops cache invalidations from the coherence network, so an L1D keeps stale lines. Plain loads then miss other tiles' updates, while atomics, which are served at the L2, see them. The cause is a Verilator scheduling bug with partial assignments to one packed struct ([verilator#5829](https://github.com/verilator/verilator/issues/5829)): the adapter drives `dcache_rtrn_o.inv.vld` and `.all` from continuous `assign`s while `p_rtrn_logic` reads them.

Upstream CVA6 resolves this in [cva6#2809](https://github.com/openhwgroup/cva6/pull/2809), which moves those fields into the always block. OpenPiton pins CVA6 at commit `4c01614f8` (2022-10-13), which predates that change, so fix 13 ports it. With the unmodified `syscalls.c` (`PATCH_SKIP="11"`), `barrier_atomic.c` on a clean 2x2 build gives these results:

| CVA6 adapter | Result |
|---|---|
| With fix 13 | pass after 14 s of simulation |
| Original, as OpenPiton pins it | timeout after 390 s of simulation |

With fix 13, OpenPiton's `hello_world_many.c`, `add_shared_var.c`, and `lrsc_test.c`, which poll a counter with plain loads, pass on a 2x2 mesh as well. Fix 11, the earlier workaround, stays in the script: the published results predate fix 13, and a checkout patched with `PATCH_SKIP="13"` reproduces them. The same issue likely explains why MACE's gate workloads read shared values through an `atomic_read()` helper.

## Build notes

- A 4x4 build compiles a large amount of generated C++. Build with `MAKEFLAGS=-j1` to stay within memory. Fix 9 removes a bare `make -j` from `sims` so that setting takes effect.
- Under Verilator 5, the adapter adds `--no-timing` to every build.
