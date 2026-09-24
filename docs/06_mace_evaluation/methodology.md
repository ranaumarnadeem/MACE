% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Methodology

The evaluation compares three ways to bring up 2x2 and 4x4 OpenPiton meshes of Ariane and PicoRV32. A run passes when Verilator simulation of the RTL reports `Simulation -> PASS (HIT GOOD TRAP)`. The finish mask has one digit per tile, so that line appears only after every tile hits its good trap.

## Approaches

| Approach | What it does | Driver |
|---|---|---|
| (a) Manual | Builds and runs the configuration by hand through the adapter, with no LLM. | `OpenPitonWorkspaceNode.build()` and `run()` called directly |
| (b) One-shot LLM | Asks the LLM once for a configuration, then builds and runs it, with no tools and no retry. | `examples/baseline_one_shot_llm.py` |
| (c) MACE loop | Plans a task graph, runs it level by level, gates each task on simulation, and replans on failure. | `examples/mace_end_to_end.py --mesh <X>x<Y>` |

All LLM calls use Gemini 2.5 Flash on Vertex AI.

## Workloads

- Ariane runs `barrier_atomic.c`: all harts meet at a barrier and add to a shared atomic counter.
- PicoRV32 runs `addi.S`. [PicoRV32](../05_mace_cores/pico.md) explains why it runs only assembly workloads.
- OpenSPARC T1 is left out. [OpenSPARC T1](../05_mace_cores/sparc.md) explains why.

PicoRV32 needs the `CONFIG_DISABLE_BIST_CLEAR` RTL define, and all three approaches get it: (a) sets it by hand, and the objectives for (b) and (c) name it. The one-shot config line has an optional `config_rtl` field, and the loop's planner requests the define through a `CONFIG_RTL:` line.

## Time basis

All approaches share one build cache, so an approach pays for a build only when it picks a new configuration. Times are end-to-end wall time, including LLM calls for (b) and (c). Manual times count machine time only.

## Environment

- One job at a time on a single OpenPiton checkout, with all fixes from `scripts/patch_openpiton.sh` applied.
- `MAKEFLAGS=-j1` for all builds, as [Ariane (CVA6)](../05_mace_cores/ariane.md) explains.
- Verilator 5.020 with `--no-timing`. The Nix shell pins Verilator 5.052.
- The one-shot baseline allows 15000 s for a build and 3600 s for a run; the loop's run limit is also 3600 s.
