% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Troubleshooting

## Stale cached builds

Each model lives in `$PITON_ROOT/build/manycore/<build_id>`, with a build ID of `mace_` plus 12 hex digits.
A successful build leaves a `.mace_build_ok` marker, and later builds with the same ID reuse the model without running `sims`.

A build ID covers the configuration, not source edits.
The loop constructs its `PitonConfig` from the spec alone, so its IDs depend only on the core, mesh, network, RTL defines, cache geometry, and extra flags.
After an RTL or monitor edit, or a new fix from `scripts/patch_openpiton.sh`, the loop still reuses models built before the change.
A typical symptom is the same failure on every replan while triage blames the RTL.

Move the stale model aside, or rebuild with `clean=True`, which first deletes the model's `obj_dir` and marker:

```bash
mv ~/openpiton/build/manycore/mace_<id> ~/openpiton/build/manycore/mace_<id>.stale
```

```python
art = get(node.build.chia_remote(cfg, clean=True))
```

The shell prints each config task's `model_dir`, and `PitonConfig` gives the ID the loop uses for a configuration:

```python
from chia_openpiton.state_def import PitonConfig

cfg = PitonConfig(core="pico", x_tiles=4, y_tiles=4,
                  config_rtl=("CONFIG_DISABLE_BIST_CLEAR", "MINIMAL_MONITORING"))
print(cfg.build_id)
```

## Ray times out during startup

On a loaded machine, `ray.init()` can fail when the raylet stops waiting for the dashboard agent, with `Timed out waiting for file .../dashboard_agent_listen_port_...`.
Delete the cluster marker and run again:

```bash
rm /tmp/ray/ray_current_cluster
```

The drivers call `ray.init(address="local", ...)`, which starts a fresh local instance even when a stale marker exists.
The `include_dashboard=False` in `examples/mace_end_to_end.py` does not prevent the race.

## Memory and MAKEFLAGS for 4x4 builds

Verilator compiles its generated C++ with parallel `cc1plus` processes of 100 MB to 850 MB each, and a 16-tile Ariane build can run out of memory.
Export `MAKEFLAGS=-j1` before a 4x4 run:

```bash
export MAKEFLAGS=-j1
```

Fix 9 of `scripts/patch_openpiton.sh` removes the bare `make -j` in `sims` that would override `MAKEFLAGS`.
Do not raise it to `-j2`, where the boot ROM `Makefile` races on `rv64_platform.dtb`.
On a `chia up` cluster, set `MAKEFLAGS` in the worker's `worker_env_commands`, as `cluster/local.yaml` does; a driver's environment does not reach remote workers.
A fresh 4x4 build at `-j1` can take over an hour, and the adapter's build timeout defaults to 7200 s.

## Verilator versions

Verilator 5 refuses OpenPiton's bare `#1` delays unless it gets `--timing` or `--no-timing`.
The adapter reads `verilator --version` inside OpenPiton's environment and adds `--no-timing` for version 5 and later; Verilator 4 has no such flag.
Do not pass `--timing`: `my_top.cpp` advances simulation time itself, and `--timing` aborts at startup with `Missed a time slot?`.
A build that fails with `%Error-NEEDTIMINGOPT` (tag `verilator_needs_timing_flag`) did not get `--no-timing`; check which `verilator` OpenPiton's environment finds through `PATH` and `VERILATOR_ROOT`.

| Version | Result with this RTL |
|---|---|
| 5.020 | Builds and runs Ariane and PicoRV32 with `--no-timing`; used for the evaluation. OpenSPARC T1 stalls. |
| 5.028 | Internal crash `Wide Op w/ no temp` in `V3EmitCFunc.cpp` (verilator#5820), fixed in 5.036. |
| 5.040, 5.048 | Their `verilated.mk` sets `CFG_CXXFLAGS_PCH` without `-c`, so every build fails to link the precompiled header with ``undefined reference to `main'`` (tag `pch_link_failure`). |
| 5.049 devel | Builds, but its `verilator_coverage` faults on `--version`. |
| 5.052 | Pinned by `flake.nix`; clears the problems above. |

## Tool name length

CHIA's API backends name each tool function `<tool>__<function>`, cut to 64 characters.
When two names collapse to the same prefix, Vertex rejects the request with `Duplicate function declaration found`.
MACE therefore names its unit-test edit tool `ut_edit_` plus 8 hex digits of a hash of the task ID.
Keep `<tool>__<function>` within 64 characters for any tool you add.

## Other build errors

| Symptom | Cause | Fix |
|---|---|---|
| `undefined reference to VerilatedCov::...` in a build without coverage | An old `#ifdef VM_COVERAGE` guard in `my_top.cpp` | Run `scripts/patch_openpiton.sh` again (fix 5). |
| Diags fail with `string.h: No such file` | A distribution `riscv64-unknown-elf-gcc` without newlib comes first on `PATH` | Set `RISCV` to your toolchain's install directory. |
| Boot ROM C23 error remains after patching | The boot ROM `clean` target leaves stale `.o` files | Delete the stale `main.o` in the boot ROM directory. |
| `/usr/bin/env: 'python3\r'`, or `dtc` fails on a path string | Checkout on a Windows-mounted drive | Clone on native Linux storage; fixes 3 and 4 repair an existing checkout. |
