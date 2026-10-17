% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Generic Tile

The generic tile runs an RV32 core that has no OpenPiton adapter of its own. The core sits in a per-core wrapper that presents one simple memory bus. A shared transducer connects that bus to the tile's L1.5. A short YAML core descriptor tells the build where the core's RTL is and what it supports. MACE selects the tile with `core="generic"` and a descriptor, which adds `-generic` and the descriptor's `-rv32_march`/`-rv32_mabi` to the `sims` command line.

```text
core RTL -- wrapper -- bridge -- simple bus -- generic_l15_transducer -- tile L1.5 wires
            (per core)  (shared)               (shared)
```

`scripts/patch_openpiton.sh` fixes 14 to 17 install the tile into a checkout; fix 18 lets a program load at the descriptor's RAM base, and fix 19 builds C programs. See [Environment Patches](../04_chia_openpiton/environment_patches.md).

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

`generic_l15_transducer.v` carries PicoRV32's request and return logic with a request state machine in place of its valid-edge detector. It stays in reset until the L1.5 leaves reset (`spc_grst_l`), several cycles after the tile's `rst_n`: the L1.5 drops a request it takes earlier, and a core such as SERV fetches in its first cycle. The core's request waits on the bus until then. Every generic build sets `CONFIG_DISABLE_BIST_CLEAR`, since a core that boots before the SRAM model's power-on clear ends loses its first writes (see [PicoRV32](pico.md)). It swaps byte order in both directions, since the L1.5 keeps data big-endian. An address with bit 31 set, or an atomic, goes non-cacheable. A store with any other `wstrb` sets `mon_error`, and the monitor reports the run as failed.

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
| `generic_bridge_wishbone.v` | One Wishbone classic master (`bus: wishbone`), such as PicoRV32's Wishbone top, `picorv32_wb` |
| `generic_bridge_wishbone2.v` | Separate instruction and data Wishbone classic masters (`bus: wishbone_split`), such as VexRiscv. Data goes before instruction, one request at a time. |

No bridge carries atomics. Both Wishbone bridges take a request when `cyc` and `stb` are both high and raise `ack` for one cycle, with the simple bus's ready. Their `adr` ports are byte addresses: a core with a word address, such as VexRiscv's 30-bit `ADR`, drives `{adr, 2'b00}`. A read is a word read. `sel` gives the byte lanes of a write. The bridges never raise `err` or `rty`.

Each core has a module named `generic_core_wrapper` with a fixed interface: `clk`, `rst_n`, `hart_id`, the simple bus as master, and `wake` (the L1.5's wake-up interrupt). The wrapper instantiates the core and its bridge and ties off the inputs the tile doesn't drive. `examples/cores/ibex/generic_core_wrapper.sv` is Ibex's, and `examples/cores/picorv32_wb/generic_core_wrapper.v` is the one-port Wishbone example.

`scripts/generic_ut/run_bridge_wishbone_ut.sh` runs a unit testbench for both Wishbone bridges against a stub simple bus, and `scripts/generic_ut/mutate_wishbone_ut.py` runs it against 24 mutated copies of the bridges, each of which it must fail.

## The core descriptor

```yaml
core: ibex
top_module: ibex_top
wrapper: ibex/generic_core_wrapper.sv
include_dirs: [ibex/vendor/lowrisc_ip/ip/prim/rtl]
sources: [ibex/rtl/ibex_pkg.sv, ibex/rtl/ibex_top.sv]
isa: {march: rv32imc, mabi: ilp32}
bus: obi                    # simple | obi | wishbone | wishbone_split
reset_vector: 0x40000080
atomics: none               # none | bus
private_cache: none         # none | disabled | write_through | write_back
hart_id: port               # port | register
```

Paths are relative to `piton/design/chip/tile/`. `ram_base` (default `0x40000000`), `ram_size` (default `0x100000`, 1 MB), `harts` (default 1), `own_interconnect` (default false), and `notes` are optional. `chia_openpiton/core_descriptor.py` reads it and writes two files into the checkout: `tile/generic/rtl/Flist.core`, which lists the core's sources and wrapper, and the test environment's `link.ld`. The descriptor's digest is part of the build ID, so two cores never share a model.

The RAM is `ram_base` up to `ram_base + ram_size`. The build passes `-rv32_image_base=<ram_base>` to `sims`, so `rv32_as` loads the program image at `ram_base` (patch fix 18). The link script starts there, puts `.text.init` at the reset vector, then `.text`, `.rodata`, `.data`, and `.bss`, and defines `__bss_start`, `__bss_end`, `_end`, and `__stack_top` (the end of RAM). The link fails if the program does not fit. A core that resets elsewhere than `0x40000000` needs a wrapper that sets its boot address to match: `examples/cores/ibex_moved.yaml` is Ibex with its RAM at `0x20000000`.

`support_problems()` checks the descriptor before any build. It rejects:

- an RV64 core;
- more than one hart;
- a write-back or write-through private cache, since the tile has no invalidation path to keep it coherent;
- a core with its own L2, L3, or NoC;
- RAM outside `0x40`-`0x7fffffff`, the addresses a 32-bit tile reaches DRAM for in the simulation (an address with bit 31 set is I/O), or a `ram_base` or `ram_size` that is not a multiple of 64, the image loader's line size;
- a reset vector that is not word-aligned or not inside RAM;
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
| 7 | C programs: `generic_c_hello.c` and `generic_c_flags.c`, plus MACE's atomic workloads for `atomics: bus` | `ladder_tests()["c"]` |

Tests run in `piton/verif/diag/assembly/include/riscv/generic/`. Its `riscv_test.h` stores to the finish register and assembles tests without compressed instructions, since the RV32I tests assume 4-byte instructions. The generic tile's own programs are in `piton/verif/diag/assembly/riscv/generic/`, beside `generic_expect_fail.S` and `generic_expect_timeout.S`, which must end in a failure and a timeout.

`scripts/generic_ut/run_generic_ut.sh` runs the transducer's unit testbench against a stub L1.5.

## C programs

A C program runs on the generic tile with the runtime in `piton/verif/diag/assembly/include/riscv/generic/`: `crt.S`, `syscalls.c`, `util.h`, and the link script. Fix 19 teaches `rv32_as` to build a `diag.c` with them. It passes the descriptor's `-rv32_march` and `-rv32_mabi`, `-nostdlib -nostartfiles`, and libgcc. The toolchain has no newlib, so it adds picolibc's headers when the toolchain has them. An assembly test is built as before. `mem.image` starts at `RV32_IMAGE_BASE`, 16 hex digits with a default of `0000000040000000`.

libgcc comes from the nearest multilib the compiler has: `rv32ima` links the `rv32ia` library and `rv32imc` the `rv32im` one. `rv32i` and `rv32im` have their own.

The link script defines `__bss_start`, `__bss_end`, and `__stack_top`. `crt.S` starts every tile:

- It clears the registers and reads the tile ID and the number of tiles from the transducer's registers.
- It gives each tile a 4 KB stack, below `__stack_top` by tile ID.
- Tile 0 clears `.bss` between `__bss_start` and `__bss_end`, then sets a flag in `.data`. The other tiles wait for it.
- It calls `main(1, argv)`. `argv[0][0]` is the tile ID and `argv[0][1]` the number of tiles, as in Ariane's runtime.
- It writes `main`'s return value to the finish register: 0 becomes 1 (pass), and any other value `v` becomes `(v << 1) | 1` (fail).

The `pass` and `fail` labels at the two stores give `rv32_as` its good-trap and bad-trap symbols. There is no trap handler, so a trap ends in a timeout.

`syscalls.c` has `exit`, `putchar`, `printstr`, a small `printf` (`%d %i %u %x %c %s %%`, a `0` flag, and a width), and the string functions gcc may call on its own. Output goes to the fake UART at `0xF0C2C000`, one character per store, and lands in `fake_uart.log` in the run directory. `util.h` has `ATOMIC_OP` and `ATOMIC_FETCH_OP` for cores with the A extension, and `read_csr`.

| Program | Where | Checks |
|---|---|---|
| `generic_c_hello.c` | `riscv/generic/` | Every hart prints its ID, one after another, and returns 0. |
| `generic_c_flags.c` | `riscv/generic/` | Hart 0 writes sixteen words, then a flag. The other harts wait for the flag and check the words. No atomics. |
| `generic_c_fail.c` | `riscv/generic/` | Returns 3. A run of it must fail. |
| `barrier_atomic.c`, `producer_consumer.c`, `scatter_gather.c` | `mace/workloads/` | MACE's gate workloads. They build unchanged and need `atomics: bus`. A run finds them through `asm_diag_root`. |

On a 2x2 mesh, PicoRV32 passes all five programs it can run and fails `generic_c_fail.c`. Ibex passes `generic_c_hello.c` and `generic_c_flags.c`, with every tile starting at its reset vector, and fails `generic_c_fail.c`.

## Adding a core

1. Copy the core's RTL under `piton/design/chip/tile/<core>/` and write its `generic_core_wrapper`. `examples/cores/ibex/install.sh` does both for Ibex.
2. Write the descriptor and run `core_descriptor check`, then `core_descriptor apply`.
3. Climb the ladder, starting with `lint_core.sh` and a 1x1 build with `-generic` and the descriptor's ISA flags.
