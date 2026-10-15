% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Generic Tile

The generic tile runs an RV32 core that has no OpenPiton adapter of its own. The core sits in a per-core wrapper that presents one simple memory bus. A shared transducer connects that bus to the tile's L1.5. A short YAML core descriptor tells the build where the core's RTL is and what it supports. MACE selects the tile with `core="generic"` and a descriptor, which adds `-generic` and the descriptor's `-rv32_march`/`-rv32_mabi` to the `sims` command line.

```text
core RTL -- wrapper -- bridge -- simple bus -- generic_l15_transducer -- tile L1.5 wires
            (per core)  (shared)               (shared)
```

`scripts/patch_openpiton.sh` fixes 14 to 17 install the tile into a checkout. See [Environment Patches](../04_chia_openpiton/environment_patches.md).

## Status

| Core | Wired | Meshes | Result |
|---|---|---|---|
| PicoRV32 | `examples/cores/picorv32.yaml`, the regression core | 1x1, 2x2, 4x2, 4x4 | PicoRV32's gate tests and the generic tile's programs pass on every mesh. At 1x1, all nine AMO tests pass. |
| Ibex (`ibex_top`, RV32IMC) | `examples/cores/ibex.yaml`, by hand | 1x1, 2x2, 4x4 | At 1x1 and 2x2, the 44 RV32I/M tests other than `simple.S` and the AMO tests pass, with the ID and shared-memory programs. At 4x4, `addi`, `beq`, `jal`, `lw`, `mul`, `div`, and those two programs pass. |

`scripts/local_generic_gate.py` runs the PicoRV32 gate through the adapter MACE uses. At 2x2, 12 of 12 verdicts matched: 10 passes, the expected fail, and the expected timeout.

On every mesh, `generic_expect_fail.S` fails and `generic_expect_timeout.S` times out, as they should.

Some of PicoRV32's tests don't fit a multicore run, and they fail on native PicoRV32 too:

- `simple.S` has no `pass` label, so `sims` finds no good trap and the run times out, on any mesh.
- The AMO tests run one read-modify-write sequence on one shared word from every tile, so they fail from 2x2 up. `generic_amo_counter.S` tests atomics across tiles instead.
- The store tests (`sb.S`, `sh.S`, `sw.S`) write different values to the same shared words in different test cases, so one tile's store can land between another tile's store and load. They fail at 4x4.

## The simple bus

PicoRV32's memory interface, made explicit:

- The master raises `valid` with `addr`, `wstrb`, `wdata`, and `amo_op`, and holds them until `ready`.
- `ready` is high for one cycle, with `rdata` for loads and atomics. The master may raise `valid` again on the next cycle.
- `wstrb` of zero is a word load. A store's `wstrb` is one of `0001`, `0010`, `0100`, `1000`, `0011`, `1100`, or `1111`, with its data in the byte lanes it selects.
- A request with a non-zero `wstrb` and an `amo_op` other than `L15_AMO_OP_NONE` is a 4-byte atomic. `amo_op` is ignored for loads: PicoRV32 leaves it set on the fetches after an atomic.

One request is outstanding at a time, so the transducer sends a core's requests to the L1.5 in the order the core issues them.

## The transducer

`generic_l15_transducer.v` carries PicoRV32's request and return logic with a request state machine in place of its valid-edge detector. It swaps byte order in both directions, since the L1.5 keeps data big-endian. An address with bit 31 set, or an atomic, goes non-cacheable. A store with any other `wstrb` sets `mon_error`, and the monitor reports the run as failed.

It answers three addresses itself, without an L1.5 request:

| Address | Access | Value |
|---|---|---|
| `0xFFFF_FF00` | read | This tile's flat ID |
| `0xFFFF_FF04` | read | The number of tiles |
| `0xFFFF_FF08` | write | Finish: 1 means pass, any other non-zero value means fail. The first non-zero write is kept. |

Every core gets its hart ID and its finish signal the same way, so test programs need no per-core code. In `pc_cmp.v.pyv`, the `RTL_GENERIC<n>` branch reports a set finish register as the program's good-trap or bad-trap address, and the existing compare, finish mask, and timeout decide the run. A program that never writes the register times out.

The transducer also prints each tile's first request address (`generic tile <n>: first request at <addr>`). `boot_problems()` in `chia_openpiton/core_descriptor.py` compares it with the descriptor's reset vector. A test pass does not show the vector is right: with Ibex's descriptor set to `0x40000000`, Ibex still starts at `0x40000080`, which skips only the test's 32 register-clearing instructions, and `addi.S` passes. `boot_problems()` reports the mismatch.

## Bridges and wrappers

| Bridge | For |
|---|---|
| none | Cores whose memory interface is the simple bus, such as PicoRV32 |
| `generic_bridge_obi.v` | Separate instruction and data ports in OBI style (req/gnt, then rvalid), such as Ibex. Data goes before instruction, one request at a time. |
| `generic_bridge_wishbone.v` | Wishbone classic masters |

Neither bridge carries atomics. Each core has a module named `generic_core_wrapper` with a fixed interface: `clk`, `rst_n`, `hart_id`, the simple bus as master, and `wake` (the L1.5's wake-up interrupt). The wrapper instantiates the core and its bridge and ties off the inputs the tile doesn't drive. `examples/cores/ibex/generic_core_wrapper.sv` is Ibex's.

## The core descriptor

```yaml
core: ibex
top_module: ibex_top
wrapper: ibex/generic_core_wrapper.sv
include_dirs: [ibex/vendor/lowrisc_ip/ip/prim/rtl]
sources: [ibex/rtl/ibex_pkg.sv, ibex/rtl/ibex_top.sv]
isa: {march: rv32imc, mabi: ilp32}
bus: obi                    # simple | obi | wishbone
reset_vector: 0x40000080
atomics: none               # none | bus
private_cache: none         # none | disabled | write_through | write_back
hart_id: port               # port | register
```

Paths are relative to `piton/design/chip/tile/`. `ram_base` (default `0x40000000`), `harts` (default 1), `own_interconnect` (default false), and `notes` are optional. `chia_openpiton/core_descriptor.py` reads it and writes two files into the checkout: `tile/generic/rtl/Flist.core`, which lists the core's sources and wrapper, and the test environment's `link.ld`, which places the program's first instruction at the reset vector. The descriptor's digest is part of the build ID, so two cores never share a model.

`support_problems()` checks the descriptor before any build. It rejects:

- an RV64 core;
- more than one hart;
- a write-back or write-through private cache, since the tile has no invalidation path to keep it coherent;
- a core with its own L2, L3, or NoC;
- RAM other than `0x40000000`, where `rv32_as` loads programs;
- a reset vector outside the first 4 KB of RAM, or not word-aligned;
- `atomics: bus` on a bus other than `simple`;
- an A extension in `isa.march` with `atomics: none`.

A MACE run with an unsupported core ends with status `unsupported_core` and its reasons, before any LLM call or build. The retry agent makes the same check.

```bash
python -m chia_openpiton.core_descriptor check examples/cores/ibex.yaml /path/to/openpiton
```

## Verification ladder

Each step runs only when the one before it passes.

| Step | Check | Tool |
|---|---|---|
| 0 | Descriptor and support check | `core_descriptor check` |
| 1 | Verilator lint of the wrapper, core, and bridges | `scripts/generic_ut/lint_core.sh` |
| 2 | Every tile's first request is at the reset vector | `boot_problems()` on `sim.log` |
| 3 | RV32I/M tests at 1x1 | `ladder_tests()["isa"]` |
| 4 | The ISA tests whose stores stay private, at 2x2 and 4x4 | `ladder_tests()["mesh"]` |
| 5 | `generic_tile_id.S` and `generic_shared_flag.S`: ID registers, and shared memory without atomics | `ladder_tests()["shared"]` |
| 6 | `generic_amo_counter.S`, for `atomics: bus` only | `ladder_tests()["atomics"]` |

Tests run in `piton/verif/diag/assembly/include/riscv/generic/`. Its `riscv_test.h` stores to the finish register and assembles tests without compressed instructions, since the RV32I tests assume 4-byte instructions. The generic tile's own programs are in `piton/verif/diag/assembly/riscv/generic/`, beside `generic_expect_fail.S` and `generic_expect_timeout.S`, which must end in a failure and a timeout.

`scripts/generic_ut/run_generic_ut.sh` runs the transducer's unit testbench against a stub L1.5.

## Adding a core

1. Copy the core's RTL under `piton/design/chip/tile/<core>/` and write its `generic_core_wrapper`. `examples/cores/ibex/install.sh` does both for Ibex.
2. Write the descriptor and run `core_descriptor check`, then `core_descriptor apply`.
3. Climb the ladder, starting with `lint_core.sh` and a 1x1 build with `-generic` and the descriptor's ISA flags.
