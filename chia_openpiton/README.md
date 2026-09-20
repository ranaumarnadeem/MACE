# chia_openpiton

A CHIA platform adapter for [OpenPiton](https://github.com/PrincetonUniversity/openpiton),
Princeton's manycore RTL platform (Ariane/CVA6 and SPARC cores), exposing
configure/build/run/collect through `OpenPitonWorkspaceNode` plus an
agent-facing MCP tool (`tools.py`). Imports **nothing** from `mace` — this
directory is self-contained so it can be dropped into an upstream CHIA
checkout as `chia/openpiton/` unchanged. Modelled on
`chia.esp.esp_workspace.EspWorkspaceNode`. Part of [MACE](../README.md).

## Install and test

From the repo root (see the top-level README for the full env setup):

```bash
pip install -e ".[test]"
pytest chia_openpiton/test -q  # tier 0: no Ray, no OpenPiton checkout needed
```

Tiers 1 (`test/cluster/`) and 2 need a real OpenPiton checkout and are gated by
env vars (`OPENPITON_TEST_RAY_ADDRESS`, `OPENPITON_TEST_REAL`, `PITON_ROOT`) —
see each test module's docstring.

## Worker requirements

A worker running these nodes needs an OpenPiton checkout and the environment
OpenPiton's own CI uses. The adapter builds that environment itself for every
command (see `_env_prefix`), but the checkout must be there:

| Resource | Meaning |
|---|---|
| `openpiton` | one worker **checkout**, not one machine — see the concurrency note below |

For the Ariane (RISC-V) core the worker additionally needs the toolchain from
`piton/ariane_build_tools.sh`: a `riscv64-unknown-elf` GCC covering
`rv64imafdc`/`lp64d`, Verilator, and (for the RV64 boot ROM, which is rebuilt at
*build* time) `dtc` and `python3`.

## Things that will bite you

These are all load-bearing, and all learned the hard way:

- **One checkout per concurrent build.** OpenPiton's template preprocessor
  writes generated `.tmp.v` files back into the *source tree* on every build, so
  two builds with different tile counts in one checkout corrupt each other. A
  worker advertising `{"openpiton": 2}` must host two separate checkouts.
- **Exit code is not success.** An RTL simulation exits 0 whether or not the
  program passed. The verdict comes from the testbench transcript
  (`Simulation -> PASS (HIT GOOD TRAP)`); `PitonRunResult.success` enforces this.
- **`piton_settings.bash` does not set `PITON_ROOT`** — it expects it exported
  already. `ARIANE_ROOT` needs its trailing slash.
- **Verilator version decides a flag.** v5 requires an explicit
  `--timing`/`--no-timing` for OpenPiton's bare `#1` delays; v4 has no such flag
  and errors if given one. OpenPiton pins **4.014**; the CHIA worker image ships
  apt's 4.038. The adapter probes the version *inside* OpenPiton's environment,
  because `ariane_setup.sh` prepends `$VERILATOR_ROOT/bin` and the Verilator a
  build uses can differ from the one a plain shell finds.
- **Always pass `-network_config` explicitly.** Left unset, `sims` defaults to
  the string `2d_mesh`, a third spelling that `pyhplib.py` does not recognise.
- **`sims -clean` never removes Verilator output** (only VCS leftovers), so
  `clean()` removes the model directory itself.
- **Every config gets its own `-build_id`**, since `sims` otherwise writes every
  model to `rel-0.1` and configurations silently overwrite each other.

## Checkout location (not optional)

Build the checkout on **native Linux storage** (e.g. `/home/you/openpiton`), not a
Windows-mounted path. `/mnt/c` is a 9p mount, and beyond being slow it showed
read-after-write coherency gaps during the boot ROM step. Measured here: an
Ariane 1x1 Verilator build takes **37 s** on ext4; the smaller SPARC design took
roughly six minutes on `/mnt/c`.

## Patching a checkout for a modern toolchain

Run this once per checkout (and in the worker image build):

```bash
scripts/patch_openpiton.sh /path/to/openpiton
```

It is idempotent and fixes two ways OpenPiton's 2019 boot ROM breaks under a
current RISC-V GCC. Both live in
`piton/design/chipset/rv64_platform/bootrom/linux/Makefile`, which hardcodes its
flags with plain `=` assignments, so neither the environment nor a `sims` flag
can override them:

- **`zicsr`/`zifencei`**: binutils 2.38+ split these out of base RV64I, so
  `csrr s2, mhartid` no longer assembles under `-march=rv64imac`.
- **C23**: GCC 15+ defaults to C23, where `void init_uart();` declares a
  function taking *no* arguments — making the boot ROM's own two-argument call a
  hard error. Pinned to `-std=gnu17`.

The second one hides: the boot ROM's `clean` target removes only the image and
the DTB, never the `.o` files, so a stale `main.o` masks the failure until
something invalidates it — a fresh checkout, a new worker, or the image.

**Diags** need no patch: pass `-rv64_march=rv64imafdc_zicsr_zifencei` through
`PitonConfig.extra_flags` and `sims` handles it.

## Status

Phase 1 acceptance, measured on this machine (Ariane, Verilator 5.049):

| # | Check | Result |
|---|---|---|
| 1 | `configure` → `build` → `run(hello_world.c)` | pass, verdict from transcript |
| 2 | 2×2 build on a GCP worker | blocked — real CHIA-side bug, filed as [ucb-bar/chia#72](https://github.com/ucb-bar/chia/issues/72) |
| 3 | Parallel builds across two checkouts | pass — 176 s vs 324 s serial |
| 4 | `chia viz` renders the example graph | pass |
| 5 | Tier-0 tests on captured fixtures | pass (`pytest chia_openpiton/test -q --ignore=chia_openpiton/test/cluster` for the current count) |

## Upstreaming checklist

Before proposing this directory as `chia/openpiton/`:

- [ ] `docs/api/openpiton.rst` — worker requirements, resource table, `automodule`
- [ ] `dockerfiles/OpenPitonDockerfile` **plus** a matching `.github/workflows/` action
- [ ] Tests under `chia/openpiton/test/`, docstrings on every public function
- [ ] Commits as `feat(openpiton): …` with `Assisted-by:` and `Signed-off-by:`
- [ ] AI-assistance disclosed in the PR description
- [ ] No edits to README / CONTRIBUTING / LICENSE / SECURITY
