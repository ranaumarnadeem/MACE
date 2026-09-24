% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Introduction

`chia_openpiton` is a CHIA adapter for OpenPiton, Princeton's manycore RTL platform. It wraps OpenPiton's `sims` tool as CHIA nodes, so an agentic loop can configure, build, simulate, and collect results from an OpenPiton manycore through one interface. MACE drives OpenPiton through it. The adapter supports three cores: [Ariane](../05_mace_cores/ariane.md), [OpenSPARC T1](../05_mace_cores/sparc.md), and [PicoRV32](../05_mace_cores/pico.md).

## What the adapter exposes

| Module | Contents |
|---|---|
| `state_def.py` | [PitonConfig](piton_config.md), the result dataclasses (`PitonBuildArtifact`, `PitonRunResult`, `PitonRegressResult`, `PitonCollectResult`), and the literal types `PitonCore`, `SimType`, `NetworkConfig`, and `Verdict` |
| `openpiton_workspace.py` | [OpenPitonWorkspaceNode](workspace_node.md), with the members `configure`, `build`, `run`, `regress`, `put_file`, `collect`, `clean`, and the `sims` escape hatch |
| `parse.py` | [Transcript parsers](parsers.md): pure functions over `sim.log`, `status.log`, and `sims` output |
| `tools.py` | [PitonToolServer](tool_server.md), the MCP tool server an LLM agent calls |

`state_def` and `parse` have no dependencies, so the config types and parsers work without Ray or MCP. `import chia_openpiton` exports `PitonConfig`, the result dataclasses, the literal types, `DEFAULT_CACHES`, and `MAX_TILES_PER_AXIS`, and adds `OpenPitonWorkspaceNode` when Ray and CHIA are installed. Each OpenPiton checkout is prepared once with `scripts/patch_openpiton.sh` (see [Environment Patches](environment_patches.md)).

## The CHIA adapter pattern

The adapter follows CHIA's ESP adapter, `chia.esp.esp_workspace.EspWorkspaceNode`, and CHIA's gem5 tool server, `chia.simulators.gem5.Gem5ToolServer`.

- **Colocated node.** `OpenPitonWorkspaceNode` subclasses CHIA's `ColocatedNode`. `sims` keeps its state on disk: a model directory under `$PITON_ROOT/build/` with the run directories inside it. Later calls read what earlier calls wrote, so `configure`, `build`, and `run` must reach the same worker. By default the node reserves a placement group with one bundle, `{"CPU": 1, "openpiton": 1}`, and pins every member call to it.
- **Resource-tagged remote functions.** Each member is a static method decorated with `@ChiaFunction(resources={"openpiton": 1})`, with the checkout root as its first argument. A node instance binds the root, so `node.build.chia_remote(cfg)` omits it and runs on the node's bundle. The class attribute, `OpenPitonWorkspaceNode.build.chia_remote(root, cfg)`, takes the root and is not pinned.
- **Tool server over the same functions.** [PitonToolServer](tool_server.md) exposes these members to an LLM agent, so the agent and the Python API share one implementation.

## The openpiton resource

`openpiton` is a custom Ray resource. One unit is one OpenPiton checkout, and one machine can host several. OpenPiton's template preprocessor (pyHP) writes generated `.tmp.v` files back into the source tree on every build, so two concurrent builds with different tile counts in one checkout corrupt each other. A worker that advertises `{"openpiton": 2}` must host two separate checkouts, each with its own node instance. MACE's parallel integrator opens one node per checkout for the same reason (see [Integrator](../03_mace_design/integrator.md)).

When no worker advertises enough `openpiton` units, the placement-group reservation waits instead of failing, unless `pg_ready_timeout_s` is set.

## Isolation from mace

`chia_openpiton` imports nothing from `mace`, so it can move into an upstream CHIA checkout as `chia/openpiton/` unchanged.

The `tier0` job in `.github/workflows/ci.yml` checks the rule on every push and pull request. It installs CHIA from source, imports the package, and fails if any `mace` module was loaded:

```bash
python -c "
import chia_openpiton, sys
leaked = sorted(m for m in sys.modules if m.startswith('mace'))
assert not leaked, f'chia_openpiton pulled in {leaked}'
print('isolation OK')
"
```

The same job then runs the tier-0 tests, which start no Ray instance and need no OpenPiton checkout. To run the adapter's tier-0 tests alone:

```bash
pytest chia_openpiton/test -q --ignore=chia_openpiton/test/cluster
```

`chia_openpiton/test/cluster/openpiton_e2e_test.py` holds tier 1 and tier 2. Tier 1 starts a local Ray instance and checks that a node reserves and releases its placement group. Tier 2 builds and runs diags on an OpenPiton checkout when `OPENPITON_TEST_REAL=1` and `OPENPITON_ROOT` are set. `chia_openpiton/README.md` keeps the upstreaming checklist.
