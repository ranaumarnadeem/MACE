# MACE

Multi-core Agentic Co-design Engine — extends [CHIA](https://github.com/ucb-bar/chia)
with support for [OpenPiton](https://github.com/PrincetonUniversity/openpiton), then
builds an agentic workflow on top for autonomous multicore construction and
verification. See [`CHIA_proposal.pdf`](CHIA_proposal.pdf) for the full proposal.

## Layout

- `chia_openpiton/` — the CHIA platform adapter. Imports **nothing** from `mace`,
  so it can be dropped into an upstream CHIA checkout as `chia/openpiton/`
  unchanged. Modelled on `chia.esp.esp_workspace.EspWorkspaceNode`.
- `mace/` — Phase 2: the agentic loop, its agents, gate workloads and metrics.
- `examples/` — runnable demos (`hello_openpiton.py` is the smallest end-to-end loop).
- `cluster/` — CHIA cluster configs (`local.yaml` today, `gcp.yaml` when credits land).
- `dockerfiles/` — the worker image.

This repo does **not** fork CHIA. CHIA is a plain dependency installed from its
own clone, the same pattern CHIA's docs describe for companion node libraries.

## Install

```bash
conda create -n chia_env -c conda-forge --override-channels python=3.10.19
conda activate chia_env

git clone https://github.com/ucb-bar/chia.git
pip install -e ./chia          # not on PyPI at the revision we build against

pip install -e ".[test]"       # this repo
pytest chia_openpiton/test -q  # tier 0: no Ray, no OpenPiton needed
```

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

## Upstreaming checklist

Before proposing `chia_openpiton/` as `chia/openpiton/`:

- [ ] `docs/api/openpiton.rst` — worker requirements, resource table, `automodule`
- [ ] `dockerfiles/OpenPitonDockerfile` **plus** a matching `.github/workflows/` action
- [ ] Tests under `chia/openpiton/test/`, docstrings on every public function
- [ ] Commits as `feat(openpiton): …` with `Assisted-by:` and `Signed-off-by:`
- [ ] AI-assistance disclosed in the PR description
- [ ] No edits to README / CONTRIBUTING / LICENSE / SECURITY
