% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Environment Patches

`scripts/patch_openpiton.sh` applies twelve numbered fixes to an OpenPiton checkout and adds one unit-test environment. The fixes edit files in the checkout because some settings have no other hook. The boot ROM `Makefile`, for example, sets its compiler flags with plain `=` assignments, which neither the environment nor a `sims` flag can override.

## Running the script

```bash
bash scripts/patch_openpiton.sh /path/to/openpiton
PITON_ROOT=/path/to/openpiton bash scripts/patch_openpiton.sh
```

Run it once per checkout, on every worker that hosts one. The GCP worker in `cluster/local.yaml` runs it from `worker_setup_commands`. The script needs `bash`, `git`, `python3`, and GNU `grep` and `sed`. It exits with status 2 when no root is given or the root has no `piton/` directory. It exits with status 1 when the boot ROM `Makefile` is missing, when a text block or anchor it edits does not appear exactly once, or when fix 5 finds a half-applied guard.

The script is idempotent. Each fix checks whether its change is already present and, if so, skips it with a message such as `already patched: <path>`. Fixes 5 to 12 skip with a `not found, skipping fix N` message when their target file is absent.

The script's edits change the checkout's source fingerprint, so a model built before the script ran is rebuilt on its next `build()` (see [OpenPitonWorkspaceNode](workspace_node.md)).

## Fixes

| # | File(s) touched | What it fixes |
|---|---|---|
| 1 | `piton/design/chipset/rv64_platform/bootrom/linux/Makefile` | binutils 2.38 and later split `zicsr` and `zifencei` out of base RV64I, so the boot ROM's `csrr s2, mhartid` no longer assembles under `-march=rv64imac`. Changes the flag to `-march=rv64imac_zicsr_zifencei`. |
| 2 | Same `Makefile` | GCC 15 and later default to C23, where `void init_uart();` declares a function with no arguments, so the boot ROM's two-argument call fails. Appends `-std=gnu17` to `CFLAGS`. `sims` runs `make clean` for this boot ROM before every build, so the flag applies to the next build. |
| 3 | Git-tracked symlinks across the checkout | A Windows-mounted checkout defaults to `core.symlinks=false`, so tracked symlinks become text files that hold their target path, and `dtc` fails on a multi-tile build. When broken links exist, sets `core.symlinks` to true and checks them out again. Does nothing when `core.symlinks` is already true. |
| 4 | Every `*.py` and `*.sh` file | A shebang line that ends in CRLF makes `/usr/bin/env` look for a program such as `python3\r`. Converts CRLF line endings to LF in each such file whose shebang line ends in `\r`. |
| 5 | `piton/tools/verilator/my_top.cpp` | The Verilator C++ driver never calls the coverage-write API, so a coverage build writes no `coverage.dat`. Adds `#include "verilated_cov.h"` and `VerilatedCov::write("coverage.dat");` under `#if VM_COVERAGE`, since Verilator always defines `VM_COVERAGE` as 0 or 1. Stops with an error on a half-applied guard. |
| 6 | `piton/design/chip/tile/pico/rtl/picorv32.v` | PicoRV32's internal `resetn` gate opens only on `pico_int`, an L15 interrupt that a bare configuration never sends, so the core never boots. Adds a `booted` register: the core starts once after reset, and a write to `32'hffffffff` still puts it to sleep until `pico_int`. |
| 7 | `piton/verif/env/manycore/pc_cmp.v.pyv` | The `RTL_PICO0` `active_thread` block is a combinational latch gated on `PICO_CORE0.pico_int`, so good-trap and bad-trap detection never fires and the run ends at max cycles. Replaces it with a clocked block that sets `active_thread` after reset, as the `RTL_ARIANE0` block does. |
| 8 | `piton/tools/src/sims/sims,2.0`; creates `piton/tools/verilator/unit_top.cpp` | The Verilator build and run paths hardcode `cmp_top` and `Vcmp_top` and ignore `-toplevel=`, so no non-manycore `-sys=` environment builds under Verilator. Makes the Verilator build and run steps honour `-toplevel=`. For a non-manycore system, the build uses `unit_top.cpp`, a generic C++ driver, in place of `my_top.cpp`, and passes the generated top-level class as `-CFLAGS -DMACE_UNIT_TOP=V<toplevel>`. |
| 9 | `piton/tools/src/sims/sims,2.0` | The Verilator build runs a bare `make -j`, which overrides `MAKEFLAGS` and can exhaust memory on a large mesh such as a 4x4 Ariane build. Removes the bare `-j` from the make line that fix 8 writes, so `MAKEFLAGS` controls parallelism. |
| 10 | `piton/verif/env/manycore/pc_cmp.v.pyv` | Under Verilator, `finish_mask` is a 32-bit `integer`, while `active_thread` and `good` widen per tile. The mask truncates past 8 tiles, so a 4x4 run passes once 8 of its 16 tiles finish. Declares `finish_mask` as `reg [31:0]` for every simulator, so the template widens it too. |
| 11 | `piton/verif/diag/assembly/include/riscv/ariane/syscalls.c` | The exit barrier polls `finish_sync0` and `finish_sync1` with plain loads, which do not reliably observe other tiles' atomic updates. On a multi-tile mesh every hart but the last spins forever. Polls through an atomic fetch-add of zero (`ATOMIC_FETCH_OP`). |
| 12 | `piton/design/chip/tile/ariane/core/cva6.sv` | CVA6's Verilator instruction tracer always opens `trace_hart_00.dasm`, so all tiles write one file. Names the file `trace_hart_<hart_id>.dasm`. |

Fixes 3 and 4 address checkouts on a Windows-mounted path, such as `/mnt/c` under WSL.

## The pico_reset_ut environment

The script also adds `pico_reset_ut`, a unit-test environment for fix 6. It creates `piton/verif/env/pico_reset_ut/` with `pico_reset_ut_top.v`, `pico_reset_ut.flist`, and an empty `test_cases/`. It writes `piton/tools/src/sims/pico_reset_ut.config` and appends `#include "pico_reset_ut.config"` to `piton/tools/src/sims/sims.config`. The testbench ties `pico_int` to 0 and checks that `picorv32` raises `mem_valid` with `mem_addr` at `PROGADDR_RESET` after reset.

The environment builds under Verilator, using fix 8. Running it, or any other non-manycore testbench such as OpenPiton's `ifu_esl_lfsr`, stops with Verilator's `Settle region did not converge` error. The cause is three macros in the shared `piton/verif/env/test_infrstrct/test_infrstrct.v` harness that place delays inside `always @*` blocks.

## Tests

`chia_openpiton/test/test_patch_openpiton.py` runs the script against a small synthetic tree: a git repository with the boot ROM `Makefile`, `picorv32.v`, `pc_cmp.v.pyv`, `syscalls.c`, and `cva6.sv`. It checks that fixes 6, 7, 10, 11, and 12 replace their exact text blocks, and that a second run changes nothing and reports `already patched`. The test holds its own copy of each original and replacement block, so an edit to the script's blocks fails the test. It cannot detect changes in upstream OpenPiton.

```bash
pytest chia_openpiton/test/test_patch_openpiton.py -q
```
