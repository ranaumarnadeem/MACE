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

## The OpenPiton adapter

Worker requirements, gotchas, the toolchain patch script, checkout-location
requirement, upstreaming checklist, and Phase 1 acceptance status all live in
[`chia_openpiton/README.md`](chia_openpiton/README.md) — that directory is
meant to be self-contained and portable into an upstream CHIA PR, so its docs
travel with it rather than living here.
