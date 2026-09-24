# MACE

MACE is an agentic system that plans, builds, and verifies multicore OpenPiton designs on the CHIA framework.
It accepts a change only when Verilator simulation of the RTL passes.

These rules apply to any AI assistant working in this repository.
The authors set them while building MACE with Claude Code; `plan.md` in this directory describes how that work ran.

## Layout

| Path | Contents |
|---|---|
| `chia_openpiton/` | CHIA adapter for OpenPiton: configure, build, run, collect |
| `mace/` | The loop (planner, integrator, triage, metrics, replay) and the `mace` CLI |
| `examples/` | The end-to-end driver and the baselines |
| `scripts/` | `patch_openpiton.sh` and standalone build and run scripts |
| `docs/` | Sphinx site, published to GitHub Pages |
| `paper/` | The 4-page paper |
| `cluster/`, `flake.nix` | CHIA cluster config and the Nix toolchain shell |

## Commands

CHIA is not on PyPI; install it from a clone first with `pip install -e /path/to/chia`.

```bash
pip install -e ".[test]"
pytest chia_openpiton/test mace/test --ignore=chia_openpiton/test/cluster --ignore=mace/test/cluster
bash scripts/patch_openpiton.sh "$PITON_ROOT"
make -C docs SPHINXOPTS="-W --keep-going"
```

- The `pytest` line runs tier 0, which needs neither Ray nor OpenPiton. Tier-1 (local Ray) and tier-2 (patched OpenPiton checkout) tests live in `*/test/cluster/`, and each file's docstring gives its run command.
- The patch script runs once per OpenPiton checkout, before its first build.
- The docs build needs `pip install -r docs/requirements.txt` and treats warnings as errors, as CI does.

## Git

- Make small commits, one change each.
- Keep a commit message to 1–3 lines: a subject, and at most one short line saying why.
- Don't push, merge, or open a pull request unless asked.
- Push review fixes to the contributor's own PR branch, so their work and the fixes merge together as one PR.

## Code

- `chia_openpiton` imports nothing from `mace`, so it can move into CHIA as `chia/openpiton/` unchanged. CI checks this.
- Each fix in `scripts/patch_openpiton.sh` has a number, is safe to run twice, and has a test in `chia_openpiton/test/test_patch_openpiton.py`.
- Run the tier-0 tests before every commit.

## Runs

- LLM calls outside tests use Vertex AI with Gemini 2.5 Flash (`--backend vertex`). The project's GCP credits pay for it, and every result in the paper and the docs used it. Tests use `FakeLLM`.
- Run one heavy build or simulation at a time, with `MAKEFLAGS=-j1`. Without it, a 16-tile Ariane build starts many parallel compiler processes and runs out of memory.
- Don't run two builds in one OpenPiton checkout at once: a build writes generated `.tmp.v` files into the source tree.
- Give baselines the same inputs as the loop. The one-shot baseline has a `config_rtl` field because the loop's planner can request RTL defines.
- Report failures and skipped steps as they happened.

## Writing

Docs, the paper, pull request descriptions, commit messages, and code comments follow @claude_docs.md.
