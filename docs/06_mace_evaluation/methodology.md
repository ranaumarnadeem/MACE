% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Methodology

The evaluation compares three ways of bringing up a multi-tile OpenPiton mesh, on 2x2 and 4x4 meshes of Ariane and PicoRV32. A result counts as a pass only when Verilator simulation of the RTL reports `Simulation -> PASS (HIT GOOD TRAP)` and `sim.log` shows one `Hit Good trap` per tile.

## Approaches

| Approach | What it does | Driver |
|---|---|---|
| (a) Manual | Builds and runs the configuration by hand through the adapter, with no LLM. | `OpenPitonWorkspaceNode.build()` and `run()` called directly |
| (b) One-shot LLM | Asks the LLM once for a configuration, applies it, and runs it, with no tools, verification, or retry. | `examples/baseline_one_shot_llm.py` |
| (c) MACE loop | Plans tasks, runs them in parallel, gates each on simulation, and replans on failure. | `examples/mace_end_to_end.py --mesh <X>x<Y>` |

Every LLM call uses Gemini 2.5 Flash on Vertex AI.

## Workloads

- Ariane runs `barrier_atomic.c`, a barrier plus shared atomic counter across all harts.
- PicoRV32 runs `addi.S`. Its OpenPiton integration has no C compiler, so C workloads do not build for it. See [PicoRV32](../05_mace_cores/pico.md).
- OpenSPARC T1 is left out, since it does not run under the Verilator 5 toolchain used here. See [OpenSPARC T1](../05_mace_cores/sparc.md).

PicoRV32 needs the `CONFIG_DISABLE_BIST_CLEAR` RTL define. All three approaches get it: (a) sets it by hand, and the objectives for (b) and (c) name it. The one-shot config line has an optional `config_rtl` field, and the loop's planner requests the define through a `CONFIG_RTL:` line.

## Time basis

All approaches share one build cache. An approach pays for a build only when it picks a configuration that has not been built before; otherwise the cached model is reused. Times are end-to-end wall time, including LLM calls for (b) and (c). Manual times count machine time only, not the time a person spends deciding what to run.

## Environment

- One job at a time on a single patched OpenPiton checkout, with every fix in `scripts/patch_openpiton.sh` applied.
- `MAKEFLAGS=-j1` for every build, to keep 4x4 builds within memory.
- Verilator 5.020 with `--no-timing`.
- The one-shot baseline allows 15000 s for a build and 3600 s for a run; the loop's run limit is also 3600 s.
