% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Baselines

Two baselines run the same core and gate workload as the loop:

- Baseline (a), manual mesh scaling: configure, build, and run a mesh by hand through the adapter.
- Baseline (b), one-shot LLM: one prompt proposes a configuration, which is built and run once, with no tools, no verification loop, and no retry.

The full loop is approach (c).
[Methodology](../06_mace_evaluation/methodology.md) describes the comparison, and [Results](../06_mace_evaluation/results.md) reports it.

## Baseline (a): manual mesh scaling

Baseline (a) drives `OpenPitonWorkspaceNode` directly:

```python
import ray
from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR

ray.init(address="local", resources={"openpiton": 1})
node = OpenPitonWorkspaceNode("/home/you/openpiton")
try:
    cfg = get(node.configure.chia_remote(x_tiles=2, y_tiles=2, core="ariane"))
    art = get(node.build.chia_remote(cfg))
    res = get(node.run.chia_remote(
        cfg, "barrier_atomic.c",
        asm_diag_root=str(WORKLOADS_DIR), rtl_timeout=RECOMMENDED_RTL_TIMEOUT,
    ))
    print(art.success, res.verdict)
finally:
    node.close()
    ray.shutdown()
```

For PicoRV32, configure with `core="pico"` and `config_rtl=("MINIMAL_MONITORING", "CONFIG_DISABLE_BIST_CLEAR")`, and run `"addi.S"`.
`examples/run_barrier_atomic.py` wraps the Ariane case with `--piton-root`, `--core` (`ariane` or `sparc`), `--x-tiles`, and `--y-tiles`.
It always runs `barrier_atomic.c` and rebuilds with `clean=True`.

## Baseline (b): one-shot LLM

`examples/baseline_one_shot_llm.py` does not import `mace.loop`, `mace.planner`, or `mace.orchestrator`.
It sends one prompt with no tools, parses one configuration from the reply, then configures, builds, and runs it through the adapter.

| Flag | Default |
|---|---|
| `--piton-root` | required |
| `--core` | `ariane`; also `sparc` or `pico` |
| `--workload` | `barrier_atomic.c` |
| `--objective` | `Verify the barrier_atomic gate workload passes on a 1x1 mesh.` |
| `--backend` | `vertex` |
| `--model` | `gemini-2.5-flash` on `vertex` |
| `--project` | none; see [LLM Backends](LLM_Backends.md) |

The script has no `--mesh` flag.
The LLM chooses `x_tiles` and `y_tiles`, so state the mesh in `--objective`.

### The CONFIG: line

The prompt asks for exactly one line in this format:

```text
CONFIG: x_tiles=<int> | y_tiles=<int> | l1i_size=<bytes> | l1i_assoc=<int> | l1d_size=<bytes> | l1d_assoc=<int> | l15_size=<bytes> | l15_assoc=<int> | l2_size=<bytes> | l2_assoc=<int> | config_rtl=<comma-separated RTL defines to add, or none>
```

The script uses the last `CONFIG:` line in the reply.
The `config_rtl` field is optional.
The script adds the listed defines to the default `MINIMAL_MONITORING`, and a missing field or `none` adds none.
Each define must match `^[A-Z][A-Z0-9_]*$`.
The script stops before any build and prints `BASELINE (one-shot): FAILED TO PARSE A CONFIG` when the reply has no `CONFIG:` line, a missing field, or a malformed define.
A value that `PitonConfig` rejects, such as a cache size of 0, stops it the same way.

A parsed configuration builds with a 15000 s timeout, and the workload runs with `rtl_timeout=RECOMMENDED_RTL_TIMEOUT`.
After the run, the script prints a summary with `status`, `llm_calls: 1`, `execution_time_s`, `compute_usd (lower bound)`, and `verdict`.
It exits with 0 only on a pass.

```bash
python examples/baseline_one_shot_llm.py \
    --piton-root ~/openpiton \
    --core pico \
    --workload addi.S \
    --objective "Verify the addi.S gate workload passes on a 2x2 pico mesh. The build needs the CONFIG_DISABLE_BIST_CLEAR RTL define."
```

## Running (b) and (c) back to back

```bash
bash scripts/local_baselines_b_and_c_test.sh ~/openpiton
```

The script activates the `chia_env` conda environment from `~/miniconda3` or `~/anaconda3`.
It runs `examples/baseline_one_shot_llm.py`, then `examples/mace_end_to_end.py`, against the same checkout, one after the other because they share it.
Output and exit codes go to `runs/baseline_b_output.log` and `runs/baseline_c_output.log` in the repository root.
It passes only `--piton-root`, so both scripts use their default core and workload.
(c) builds its default 1x1 mesh, and (b)'s default objective asks for one.
Always pass the checkout path, because the built-in default is a path on the authors' machine.
For 2x2 and 4x4 comparisons, run the two scripts directly with matching flags.

## Seeded-failure runs

No run in [Results](../06_mace_evaluation/results.md) needed a replan.
`examples/recovery_seeded.py` breaks the loop's first plan, so the detect, diagnose, and replan cycle runs against Verilator builds and simulations:

```bash
export GOOGLE_CLOUD_PROJECT=<your-gcp-project> MAKEFLAGS=-j1
python examples/recovery_seeded.py --piton-root ~/openpiton --core ariane --mesh 2x2 \
    --workload barrier_atomic.c --fault build --runs 3
```

- `--fault build` adds `PITON_FPGA_SYNTH`, a define for FPGA synthesis, to every config and workload task in the first plan. The Verilator build then fails with `%Error-PINNOTFOUND`.
- `--fault sim` removes `CONFIG_DISABLE_BIST_CLEAR` from the first plan. It needs `--core pico`, whose simulation then fails.

Later plans are left alone.
The script replaces `mace.orchestrator.plan` for each run, prints each iteration's results and diagnoses, and records the runs in `runs/recovery_seeded.db`.
`--objective` sets the objective. Pass the one from the run being compared: the PicoRV32 runs in Results named `CONFIG_DISABLE_BIST_CLEAR` in theirs.
