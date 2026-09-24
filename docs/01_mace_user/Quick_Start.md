% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Quick Start

This page runs the MACE loop once on a 2x2 Ariane mesh with `examples/mace_end_to_end.py`.
It assumes you finished [Installation](Installation.md): the Python environment is active, and `~/openpiton` is a patched OpenPiton checkout.
Run every command from the root of the MACE repository.

## Set up the LLM backend

The script uses the Vertex Gemini backend by default.
Log in with Application Default Credentials and name the GCP project to use:

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=<your-gcp-project>
```

[LLM Backends](LLM_Backends.md) covers the other backends.

## Run the loop

```bash
python examples/mace_end_to_end.py \
    --piton-root ~/openpiton \
    --core ariane \
    --mesh 2x2 \
    --workload barrier_atomic.c \
    --backend vertex \
    --max-iterations 3 \
    --db-path runs/mace_end_to_end.db
```

`--mesh` sets the size of every `config` and `workload` build; the objective text does not.
With no `--objective`, the script writes one from the workload and mesh, here `Verify the barrier_atomic.c gate workload passes on a 2x2 mesh.`
Each configuration compiles its Verilator model once, and later runs reuse it.

## Flags

| Flag | Default | Purpose |
|---|---|---|
| `--piton-root` | required | OpenPiton checkout. |
| `--piton-root-2` | none | Second checkout; independent tasks in one DAG level run on the two checkouts in parallel. |
| `--core` | `ariane` | `ariane`, `sparc`, or `pico`. |
| `--mesh` | `1x1` | Target mesh as `XxY`, such as `2x2` or `4x4`. |
| `--workload` | `barrier_atomic.c` | Gate workload that `config` and `workload` tasks simulate. |
| `--objective` | generated | Objective text for the planner. |
| `--backend` | `vertex` | LLM backend. |
| `--model` | `gemini-2.5-flash` on `vertex` | Model name; other backends use their own default. |
| `--project` | none | GCP project for `vertex`. It overrides `GOOGLE_CLOUD_PROJECT`, and `vertex` needs one of the two. |
| `--max-iterations` | `3` | Iteration cap. The USD and wall-clock caps keep their `Budget` defaults. |
| `--db-path` | `runs/mace_end_to_end.db` | Run database. The default resolves against the current directory. |

## Read the output

The script prints the run ID and status, one block per iteration, and the five summary metrics.
A run that ends `failed` or `budget_exceeded` also prints its post-mortem.
The exit code is 0 only for a passed run.

```text
run_id=<run_id> status=passed

--- iteration 0 ---
  <task_id> (<kind>): passed=True build.success=True verdict=pass

--- summary (db=runs/mace_end_to_end.db) ---
  successful_tasks: <n>
  iterations: <n>
  failures_recovered: <n>
  execution_time_s: <seconds>
  compute_usd: 0.0
```

`compute_usd` reads 0.0 on Vertex because the backend reports no cost.
`mace results` lists past runs, and `--trace` shows one run's plan, dispatch, and triage:

```bash
mace results --db-path runs/mace_end_to_end.db
mace results --db-path runs/mace_end_to_end.db --run-id <run_id> --trace
```

## Other cores and meshes

For PicoRV32, run `addi.S` and name the `CONFIG_DISABLE_BIST_CLEAR` RTL define in the objective:

```bash
python examples/mace_end_to_end.py \
    --piton-root ~/openpiton \
    --core pico \
    --mesh 2x2 \
    --workload addi.S \
    --objective "Verify the addi.S gate workload passes on a 2x2 pico mesh in a single task. That task needs the CONFIG_DISABLE_BIST_CLEAR RTL define."
```

Before a 4x4 run, export `MAKEFLAGS=-j1` to limit the memory the Verilator C++ compile uses; [Troubleshooting](Troubleshooting.md) explains why.
[Running the Loop](Running_the_Loop.md) covers the spec, the budget, and planner directives.
