% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Installation

MACE needs a Python 3.10 environment with CHIA and MACE installed, Verilator, a RISC-V GCC toolchain, and a patched OpenPiton checkout.
The Nix flake provides Verilator and the toolchain.
The conda route uses tools you install yourself.
Both routes finish with the OpenPiton checkout and patch steps at the end of this page.

## Option 1: Nix flake

Clone MACE and enter the development shell. This needs Nix with flakes enabled.

```bash
git clone https://github.com/ranaumarnadeem/MACE.git
cd MACE
nix develop
```

The shell provides:

- Python 3.10 with `pip` and `virtualenv`.
- Verilator 5.052, taken from its own nixpkgs pin.
- The prebuilt `riscv64-unknown-elf` GCC toolchain, release 2026.08.27, exported as `RISCV` and added to `PATH`.
- The system packages OpenPiton's scripts expect, including `tcsh`, `dtc`, `perl`, `bison`, `flex`, and `gnumake`.

On first entry the shell creates and activates `.venv`.
CHIA is not part of the Nix closure, so install it and MACE into the venv yourself:

```bash
git clone https://github.com/ucb-bar/chia.git /path/to/chia
pip install -e /path/to/chia
pip install -e '.[test]'
```

Later entries reuse `.venv`.
When `PITON_ROOT` names a checkout, the shell also reports whether `scripts/patch_openpiton.sh` has been applied to it.

To use only the pinned Verilator in another environment, build the flake's `verilator` package and put it first on `PATH`:

```bash
VERILATOR_STORE=$(nix build --no-link --print-out-paths .#verilator)
export VERILATOR_ROOT="$VERILATOR_STORE/share/verilator"
export PATH="$VERILATOR_STORE/bin:$PATH"
```

## Option 2: conda and pip

```bash
conda create -n chia_env -c conda-forge --override-channels python=3.10.19
conda activate chia_env

git clone https://github.com/ucb-bar/chia.git
pip install -e ./chia
pip install -e ".[test]"
```

MACE requires Python `>=3.10,<3.11`.
CHIA is not on PyPI, and MACE tracks its main branch with no pinned commit.

Install the toolchain yourself: Verilator, a `riscv64-unknown-elf` GCC that covers `rv64imafdc`/`lp64d`, and `dtc` and `python3` for the RV64 boot ROM.
Set `RISCV` to the toolchain's install directory.
For Ariane builds the adapter puts `$RISCV/bin` first on `PATH`, and uses `$HOME/scratch/riscv_install` when `RISCV` is unset.
PicoRV32 needs no separate compiler, because the adapter drives the same `riscv64-unknown-elf-gcc` for `rv32ima`/`ilp32`.
[Troubleshooting](Troubleshooting.md) lists Verilator versions that fail with this RTL.

## Check the Python install

```bash
pytest chia_openpiton/test mace/test -q \
    --ignore=chia_openpiton/test/cluster --ignore=mace/test/cluster
```

These tests need no Ray cluster and no OpenPiton checkout.
`mace init` checks the toolchain on `PATH`; see [Interactive Shell](Interactive_Shell.md).

## Clone OpenPiton

```bash
git clone https://github.com/PrincetonUniversity/openpiton.git ~/openpiton
cd ~/openpiton
git checkout 1c6bfd2
git submodule update --init --recursive piton/design/chip/tile/ariane
```

Keep the checkout on native Linux storage, such as `~/openpiton` inside WSL, not on a Windows-mounted path such as `/mnt/c`.
Builds on `/mnt/c` are slow and have shown read-after-write coherency gaps during the boot ROM step.

A checkout holds one build at a time, because OpenPiton writes generated `.tmp.v` files into the source tree during a build.
For parallel runs with `--piton-root-2`, clone a second checkout.

## Patch the checkout

```bash
bash scripts/patch_openpiton.sh ~/openpiton
```

With no argument, the script uses `PITON_ROOT`.
It is idempotent, so running it again is safe.
It applies twelve fixes:

| Fix | File | Change |
|---|---|---|
| 1 | boot ROM `Makefile` | Adds `zicsr_zifencei` to `-march` for binutils 2.38 and later. |
| 2 | boot ROM `Makefile` | Pins `-std=gnu17`, since GCC 15 and later default to C23. |
| 3 | git checkout | Restores git symlinks that a Windows-mounted checkout stored as plain files. |
| 4 | `*.py`, `*.sh` | Converts scripts whose shebang line ends in CRLF to LF line endings. |
| 5 | `my_top.cpp` | Writes `coverage.dat` at exit when `VM_COVERAGE` is nonzero. |
| 6 | `picorv32.v` | Lets PicoRV32 boot out of reset without an L15 interrupt. |
| 7 | `pc_cmp.v.pyv` | Marks the PicoRV32 thread active in the manycore monitor. |
| 8 | `sims,2.0` | Honors `-toplevel=` in Verilator builds of unit-test environments, and adds `unit_top.cpp`. |
| 9 | `sims,2.0` | Removes a bare `make -j`, so `MAKEFLAGS` controls the C++ compile. |
| 10 | `pc_cmp.v.pyv` | Widens `finish_mask`, so meshes above 8 tiles are checked in full. |
| 11 | Ariane `syscalls.c` | Polls the multi-hart exit barrier with atomic reads. |
| 12 | CVA6 `cva6.sv` | Writes one `trace_hart_<id>.dasm` file per tile. |

The script also adds `pico_reset_ut`, a standalone unit-test environment for PicoRV32's reset behavior.
Models built before a fix are not rebuilt on their own; see [Troubleshooting](Troubleshooting.md).
[Environment Patches](../04_chia_openpiton/environment_patches.md) describes each fix in detail.
