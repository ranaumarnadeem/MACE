% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Running the Loop

A loop run is described by a frozen `MaceSpec` from `mace/spec.py`.
`examples/mace_end_to_end.py` and `mace shell` each build one and pass it to `mace.orchestrator.run_mace_loop`.

## MaceSpec

| Field | Default | Rule |
|---|---|---|
| `workloads` | required | Non-empty tuple of gate workload file names. |
| `objective` | required | Non-empty text for the planner. |
| `core` | `"ariane"` | `"ariane"`, `"sparc"`, or `"pico"`. |
| `target_mesh` | `(1, 1)` | `(x_tiles, y_tiles)`, each from 1 to 256. |
| `budget` | `Budget()` | Stop conditions; see below. |
| `coverage` | `False` | Adds Verilator line coverage to every build; see [Code Coverage](Code_Coverage.md). |

`target_mesh` sets `x_tiles` and `y_tiles` for every task's build.
The planner sees the mesh in its prompt but cannot change it.

## Gate workloads

A `config` or `workload` task runs an LLM turn and a build, then, if the build succeeded, simulates `workloads[0]`, the only workload the loop runs.
It passes when the simulator transcript reports the verdict `pass`.
A `unit_test` task builds a standalone testbench for one RTL module and is gated on that build alone.

`mace/workloads/` holds three RISC-V C gate programs:

| Workload | Check |
|---|---|
| `barrier_atomic.c` | Every hart adds to one shared atomic counter between two barriers; the count must equal the number of harts. |
| `producer_consumer.c` | Hart 0 publishes values behind per-slot ready flags; the highest-numbered hart reads and checksums them. |
| `scatter_gather.c` | Each hart writes its ID to its own slot; hart 0 gathers and checksums the array. |

Before any LLM call or checkout access, `run_mace_loop` checks these files against `mace/workloads/CHECKSUMS` and stops with status `checksum_mismatch` on any difference.
Update `CHECKSUMS` when you add or change a workload; `sha256sum -c CHECKSUMS` in `mace/workloads/` checks it.

The loop passes `mace/workloads/` to `sims` as an extra diag directory, so OpenPiton's own diags also resolve by name.
Use `addi.S` for PicoRV32, which has an assembler but no C compiler in OpenPiton.
The C workloads use RISC-V inline assembly and do not run on OpenSPARC T1.

## Simulation timeout

Every loop simulation runs with `-rtl_timeout=1000000` (`RECOMMENDED_RTL_TIMEOUT` in `mace/workloads.py`) at every mesh size, and no flag changes it.
OpenPiton's default of 50,000 cycles cuts `producer_consumer.c` short.

## Budget

| Field | Default | Cap |
|---|---|---|
| `max_iterations` | `10` | Plan, execute, and triage cycles. |
| `max_usd` | `20.0` | Summed LLM cost in USD. |
| `max_wall_s` | `3600` | Elapsed seconds since the run started. |

The caps are independent and must be positive.
The loop checks them before each iteration, so an iteration in progress always finishes, and a run that spends every iteration without a pass ends with status `budget_exceeded`.
The USD tally stays at 0 on Vertex ([LLM Backends](LLM_Backends.md)), and `examples/mace_end_to_end.py` sets only `max_iterations`.

## Planner directives

The planner replies with footer lines: `TASK:` lines form the DAG, and `CACHES:` and `CONFIG_RTL:` lines change one task's build.

```text
TASK: <id> | deps=<comma-separated task ids, or empty> | kind=config|workload|unit_test | <short instruction>
CACHES: <task id> | <name>=<size>,<associativity> ...
CONFIG_RTL: <task id> | <FLAG1> <FLAG2> ...
```

Task LLM calls get no tools, except a testbench editor for `unit_test` tasks, so these directives are how a task changes its build.
`CACHES:` accepts `l1i`, `l1d`, `l15`, and `l2`, with defaults `l1i=16384,4 l1d=8192,4 l15=8192,4 l2=65536,4`.

### CONFIG_RTL

`CONFIG_RTL:` adds RTL defines to one task's build as `-config_rtl=<FLAG>` arguments to `sims`.
They add to the default `MINIMAL_MONITORING` and never replace it.
Each flag must match `^[A-Z][A-Z0-9_]*$`, other tokens are dropped, and several lines for one task merge.

- Nothing checks a define against the RTL, so an unknown define is accepted and costs a full build.
- Each task builds its own configuration, so a define on one task does not carry over to tasks that depend on it. To apply a define once, ask for a single task in the objective.
- The planner can request a define named in the objective or in triage feedback. Name `CONFIG_DISABLE_BIST_CLEAR` in a PicoRV32 objective.

## Driving the loop from Python

```python
import ray

from mace.llm import make_llm
from mace.metrics import open_db, summary
from mace.orchestrator import run_mace_loop
from mace.spec import Budget, MaceSpec

ray.init(address="local", resources={"openpiton": 1, "vertex_creds": 1}, include_dashboard=False)
spec = MaceSpec(
    workloads=("barrier_atomic.c",),
    objective="Verify the barrier_atomic.c gate workload passes on a 2x2 mesh.",
    core="ariane",
    target_mesh=(2, 2),
    budget=Budget(max_iterations=3),
)
llm = make_llm("vertex", model="gemini-2.5-flash")
db = open_db("/home/you/MACE/runs/mace_end_to_end.db", ray_placement=False)
result = run_mace_loop(("/home/you/openpiton",), spec, llm, db)
print(result.run_id, result.status, summary(db, result.run_id))
```

Declare one `openpiton` and one `<backend>_creds` resource per checkout, and set `GOOGLE_CLOUD_PROJECT` for Vertex.
`result.post_mortem` holds the assessment of a run that did not pass, and [Results and Metrics](Results_and_Metrics.md) lists the statuses.
