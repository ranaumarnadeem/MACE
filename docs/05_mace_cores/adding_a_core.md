% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Adding a Core

OpenPiton has no reusable core-to-NoC bridge. Under BYOC, each core reaches the tile's L1.5 through its own transducer. A new core needs RTL in OpenPiton and a small amount of code in MACE.

## What a core needs in OpenPiton

The three supported cores share one shape:

- A per-core RTL directory under `piton/design/chip/tile/`.
- A transducer between the core's memory interface and the L1.5. Examples are T1's CCX transducer, Ariane's L1.5 adapter inside CVA6, and PicoRV32's `pico_l15_transducer.v`.
- A core-select arm in the tile template, plus `sims` support for selecting the core.

The core's cache decides the size of the job. A core with its own coherent cache, such as Ariane, needs coherence-adapter RTL. A core without one, such as PicoRV32, needs only a transducer. A core with no upstream adapter, such as Ibex or Rocket Chip, needs one written first.

The core also needs a way to run a workload: an assembler or compiler for its ISA, and entries in `piton/verif/env/manycore/pc_cmp.v.pyv` so the manycore monitor detects its good and bad traps.

## What a core needs in MACE

Adding PicoRV32 took these changes:

1. Add the core name to `PitonCore` and to the core check in `PitonConfig.__post_init__()`, both in `chia_openpiton/state_def.py`.
2. Add its `sims` selection flags in `PitonConfig.sims_flags()`.
3. Add the name to the core check in `MaceSpec.__post_init__()` (`mace/spec.py`), to `SUPPORTED_CORES` in `mace/cli/session.py`, and to the `--core` choices of `examples/mace_end_to_end.py` and `examples/baseline_one_shot_llm.py`.
4. Mirror the existing cores' tests in `chia_openpiton/test/`.

Changes the core needs in the OpenPiton checkout go into `scripts/patch_openpiton.sh` as new numbered fixes. Each one is safe to run twice, and a new fix gets a test in `chia_openpiton/test/test_patch_openpiton.py`. See [Environment Patches](../04_chia_openpiton/environment_patches.md).

## Bring-up checklist

1. Build a single tile and confirm the build succeeds.
2. Run a single-hart assembly test, such as the core's `addi.S` equivalent, and check `sim.log` for `Hit Good trap`.
3. If the run ends with the `maxcycles` verdict, rule out a build or toolchain problem before you look at the RTL. Compare the transcript with a passing one, and check the diag binary's symbols against `symbol.tbl`. The failure-analysis tools `compare_to_fixture` and `symbol_check` automate both checks. See [PitonToolServer](../04_chia_openpiton/tool_server.md).
4. Scale to 2x2 and 4x4 and confirm one `Hit Good trap` per tile.

## Planned work

The next step for MACE is turning this shape into a template and adding more cores, with the loop verifying each revision of the adapter RTL in Verilator while an engineer writes it.
