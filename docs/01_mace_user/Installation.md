% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Installation

MACE needs a Python 3.10 environment with CHIA installed, Verilator, a RISC-V GCC toolchain, and a patched OpenPiton checkout.
The Nix flake provides all of it except OpenPiton, CHIA and MACE included.
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

On first entry the shell creates and activates `.venv`, then installs CHIA's v1.0.1 release from PyPI and MACE, editable with its test and eval extras. The `eval` extra adds Optuna, which the co-design Bayesian search uses.
This needs network access and takes a few minutes.
Later entries reuse `.venv` and install only a package it lacks, so a first entry interrupted by the network finishes on the next one.
To install CHIA from another source, such as a local checkout, set `MACE_CHIA_SOURCE` to anything `pip install` accepts before the first entry:

```bash
MACE_CHIA_SOURCE=/path/to/chia nix develop
```

That copies the checkout into `.venv`. To edit CHIA while you work, run `pip install -e /path/to/chia` inside the shell instead.
When `PITON_ROOT` names a checkout, the shell also checks it for fix 5 of `scripts/patch_openpiton.sh` and reports whether the checkout is patched.
The `nix shell` workflow in `.github/workflows/nix.yml` enters the shell on a clean GitHub runner whenever `flake.nix`, `flake.lock`, or `pyproject.toml` changes.
It checks that the second entry installs nothing, builds and runs a small Verilator model, compiles for Ariane and PicoRV32 with the RISC-V GCC, and runs the tier-0 tests.

To use only the pinned Verilator in another environment, build the flake's `verilator` package and put it first on `PATH`:

```bash
VERILATOR_STORE=$(nix build --no-link --print-out-paths .#verilator)
export VERILATOR_ROOT="$VERILATOR_STORE/share/verilator"
export PATH="$VERILATOR_STORE/bin:$PATH"
```

## Option 2: conda and pip

```bash
git clone https://github.com/ranaumarnadeem/MACE.git
cd MACE
conda create -n chia_env -c conda-forge --override-channels python=3.10.19
conda activate chia_env

pip install chialoops==1.0.1
pip install -e ".[test]"
```

MACE requires Python `>=3.10,<3.11`.
CHIA's PyPI distribution is named `chialoops`, and its 1.0.1 wheel holds the same Python files as CHIA's v1.0.1 git tag, which every result used.
To edit CHIA, clone that tag with `git clone --branch v1.0.1 https://github.com/ucb-bar/chia.git` and install the clone with `pip install -e /path/to/chia` in place of the `chialoops` line.
To run the co-design Bayesian search, install the `eval` extra as well: `pip install -e ".[test,eval]"`.

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

Keep the checkout on native Linux storage, such as `~/openpiton` inside WSL.
Builds on a Windows-mounted path such as `/mnt/c` are slow and have shown read-after-write coherency gaps during the boot ROM step.

A checkout holds one build at a time, because OpenPiton writes generated `.tmp.v` files into its source tree while it builds.
For parallel runs with `--piton-root-2`, clone a second checkout.

## Patch the checkout

```bash
bash scripts/patch_openpiton.sh ~/openpiton
```

With no argument, the script uses `PITON_ROOT`.
Running it again is safe.
It applies thirteen fixes:

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
| 13 | CVA6 `wt_l15_adapter.sv` | Stops the L1.5 adapter from dropping cache invalidations under Verilator 5, as upstream cva6#2809 does. |

The script also adds `pico_reset_ut`, a standalone unit-test environment for PicoRV32's reset behavior.
The loop keeps reusing models built before a fix; [Troubleshooting](Troubleshooting.md) shows how to rebuild them.
[Environment Patches](../04_chia_openpiton/environment_patches.md) describes each fix in detail.
