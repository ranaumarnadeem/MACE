% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Adding a Core

OpenPiton has no reusable core-to-NoC bridge. Under BYOC, each core reaches the tile's L1.5 through its own transducer, so adding a core means adding RTL on the OpenPiton side and a small amount of code on the MACE side.

## What a core needs in OpenPiton

The three supported cores share one shape:

- A per-core RTL directory under `piton/design/chip/tile/`.
- A transducer between the core's memory interface and the L1.5. Examples are T1's CCX transducer, Ariane's L1.5 adapter inside CVA6, and PicoRV32's `pico_l15_transducer.v`.
- A core-select arm in the tile template, plus `sims` support for selecting the core.

Whether the core has its own coherent cache decides the size of the job. A core with a coherent cache, as Ariane has, needs coherence-adapter RTL. A core without one, as PicoRV32 is, needs only a transducer. A core with no upstream transducer at all, such as Ibex or Rocket Chip, needs one written first.

The core also needs a way to run a workload: an assembler or compiler for its ISA, and entries in the manycore monitor (`piton/verif/env/manycore/pc_cmp.v.pyv`) so it can detect good and bad traps.

## What a core needs in MACE

Adding PicoRV32 took these changes in `chia_openpiton`:

1. Add the core name to `PitonCore` in `chia_openpiton/state_def.py`.
2. Add its `sims` selection flags in `PitonConfig.sims_flags()`.
3. Mirror the existing cores' tests in `chia_openpiton/test/`.

Any fixes the core needs in the OpenPiton checkout go into `scripts/patch_openpiton.sh` as new idempotent fixes. See [Environment Patches](../04_chia_openpiton/environment_patches.md).

## Bring-up checklist

1. Build a single tile and confirm the build succeeds.
2. Run a single-hart assembly test, such as the core's `addi.S` equivalent, and check `sim.log` for `Hit Good trap`.
3. If the run ends at `maxcycles`, compare its transcript with a passing run and check the diag binary's symbols against `symbol.tbl` to rule out a build or toolchain problem before looking at the RTL. The failure-analysis tools `compare_to_fixture` and `symbol_check` automate both checks. See [PitonToolServer](../04_chia_openpiton/tool_server.md).
4. Scale to 2x2 and 4x4 and confirm one `Hit Good trap` per tile.

## Planned work

The next step for MACE is turning this shape into a template and adding more cores, with the loop verifying each revision of the adapter RTL in Verilator while an engineer writes it.
