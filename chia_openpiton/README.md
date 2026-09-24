# chia_openpiton

A CHIA platform adapter for [OpenPiton](https://github.com/PrincetonUniversity/openpiton),
Princeton's manycore RTL platform with Ariane (CVA6), OpenSPARC T1, and
PicoRV32 cores. `OpenPitonWorkspaceNode` exposes configure, build, run, and
collect, and `PitonToolServer` (`tools.py`) offers them to agents as MCP tools.
The directory imports nothing from `mace`, so it can move into a CHIA checkout
as `chia/openpiton/` unchanged. It is modeled on
`chia.esp.esp_workspace.EspWorkspaceNode` and is part of [MACE](../README.md).
The [chia_openpiton Adapter](https://ranaumarnadeem.github.io/MACE/04_chia_openpiton/intro.html)
part of the MACE documentation describes it in full.

## Install and test

From the repository root, after the setup in the top-level README:

```bash
pip install -e ".[test]"
pytest chia_openpiton/test -q --ignore=chia_openpiton/test/cluster
```

These tier-0 tests start no Ray instance and need no OpenPiton checkout. The
tests in `test/cluster/` add tier 1, which checks node placement on a local Ray
instance, and tier 2, which builds and runs diags on a patched checkout when
`OPENPITON_TEST_REAL=1` and `OPENPITON_ROOT` are set. The test module's
docstring gives the full commands.

## Worker requirements

A worker needs an OpenPiton checkout and the environment OpenPiton's own CI
uses. The adapter builds that environment for every command (`_env_prefix`),
but the checkout must already exist.

| Resource | Meaning |
|---|---|
| `openpiton` | One checkout on the worker. A worker that advertises `{"openpiton": 2}` hosts two checkouts. |

Ariane needs a `riscv64-unknown-elf` GCC that covers `rv64imafdc`/`lp64d`,
Verilator, and `dtc` and `python3` for the RV64 boot ROM, which is rebuilt at
build time. PicoRV32 uses the same `riscv64-unknown-elf-gcc`, targeting
`rv32ima`/`ilp32`, so it needs no separate compiler. Every checkout needs
`scripts/patch_openpiton.sh` before its first build.

## Design rules

- One build per checkout at a time. OpenPiton's template preprocessor writes
  generated `.tmp.v` files into the source tree during a build, so two builds in
  one checkout corrupt each other.
- The verdict comes from the testbench transcript. A simulation exits 0 whether
  or not the program passed, so `PitonRunResult.success` requires
  `Simulation -> PASS (HIT GOOD TRAP)`.
- `piton_settings.bash` does not set `PITON_ROOT`, so the adapter exports it
  first. `ARIANE_ROOT` keeps its trailing slash.
- The Verilator version decides one flag. Verilator 5 needs `--timing` or
  `--no-timing` for OpenPiton's bare `#1` delays, and Verilator 4 rejects both.
  The adapter reads the version inside OpenPiton's environment, which can find a
  different `verilator` than a plain shell, and adds `--no-timing` for version 5.
- `-network_config` is always passed. Left unset, `sims` uses the spelling
  `2d_mesh`, which `pyhplib.py` does not recognize.
- `sims -clean` removes only VCS output, so `clean()` deletes the model
  directory itself.
- Each configuration gets its own `-build_id`. Without one, `sims` writes every
  model to `rel-0.1`, and configurations overwrite each other.

## Checkout location

Keep the checkout on native Linux storage, such as `/home/you/openpiton`,
instead of a Windows-mounted path. `/mnt/c` is a 9p mount: builds there are slow
and have shown read-after-write coherency gaps during the boot ROM step.

## Patching a checkout

```bash
bash scripts/patch_openpiton.sh /path/to/openpiton
```

The script applies twelve numbered fixes and is safe to run again.
[Environment Patches](https://ranaumarnadeem.github.io/MACE/04_chia_openpiton/environment_patches.html)
lists them.

## Upstreaming checklist

Before proposing this directory as `chia/openpiton/`:

- [ ] `docs/api/openpiton.rst`: worker requirements, resource table, `automodule`
- [ ] `dockerfiles/OpenPitonDockerfile` and a matching `.github/workflows/` action
- [ ] Tests under `chia/openpiton/test/`, and docstrings on every public function
- [ ] Commits as `feat(openpiton): …` with `Assisted-by:` and `Signed-off-by:`
- [ ] AI assistance disclosed in the PR description
- [ ] No edits to README, CONTRIBUTING, LICENSE, or SECURITY
