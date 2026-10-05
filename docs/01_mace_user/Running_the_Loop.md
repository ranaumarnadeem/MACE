% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Running the Loop

A frozen `MaceSpec` from `mace/spec.py` describes a loop run.
`examples/mace_end_to_end.py` and `mace shell` each build one and pass it to `mace.orchestrator.run_mace_loop`.

## MaceSpec

| Field | Default | Rule |
|---|---|---|
| `workloads` | required | Non-empty tuple of gate workload file names. |
| `objective` | required | Non-empty text for the planner. |
| `core` | `"ariane"` | `"ariane"`, `"sparc"`, or `"pico"`. |
| `target_mesh` | `(1, 1)` | `(x_tiles, y_tiles)`, each from 1 to 256. |
| `budget` | `Budget()` | Stop conditions; see below. |
| `coverage` | `False` | Adds Verilator line coverage to every `config` and `workload` build; see [Code Coverage](Code_Coverage.md). |
| `rtl_timeout` | `None` | OpenPiton's `TIMEOUT` limit, which caps a gate workload's simulation at this many cycles; `None` uses `RECOMMENDED_RTL_TIMEOUT` (1,000,000). Larger meshes need more. |
| `max_cycle` | `None` | The most cycles a gate workload's simulation may take before it ends with verdict `maxcycles`; `None` keeps OpenPiton's limit. |

`target_mesh` sets `x_tiles` and `y_tiles` for every `config` and `workload` build.
The planner sees the mesh in its prompt but cannot change it.

## Gate workloads

A `config` or `workload` task runs one LLM turn and one build.
If the build succeeds, the task simulates each entry of `workloads` in order on that build.
It passes when every one reports `pass`, and it stops at the first that does not.
A `unit_test` task builds a standalone testbench for one RTL module and is gated on that build alone.

`mace/workloads/` holds four RISC-V C gate programs:

| Workload | Check |
|---|---|
| `barrier_atomic.c` | Every hart adds to one shared atomic counter between two barriers, then every hart checks that the count equals the number of harts. |
| `producer_consumer.c` | Hart 0 publishes values behind per-slot ready flags; the highest-numbered hart reads and checksums them. |
| `scatter_gather.c` | Each hart writes its ID plus one to its own slot; after a barrier, every hart gathers and checksums the array. |
| `matmul.c` | The harts share out the rows of a 16x56 by 56x56 matrix product, and every hart checks each element it computes against a closed form. For every row, a hart walks all of the 12.25 KB second matrix, which is larger than the default L1D, so the co-design searches use it. |

A hart that finds a wrong value returns nonzero and ends at the bad trap, so a run passes only when every hart saw the right result; only hart 0 prints.

Before any LLM call or checkout access, `run_mace_loop` checks these files against `mace/workloads/CHECKSUMS` and stops with status `checksum_mismatch` on any difference.
Update `CHECKSUMS` when you add or change a workload; `sha256sum -c CHECKSUMS` in `mace/workloads/` checks it.

The loop adds `mace/workloads/` to the diag directories `sims` searches, so OpenPiton's own diags still resolve by name.
Use `addi.S` for PicoRV32, which has an assembler but no C compiler in OpenPiton.
The C workloads use RISC-V inline assembly and do not run on OpenSPARC T1.

## Simulation limits

OpenPiton's testbench ends a simulation that has not passed in one of two ways.
Its `TIMEOUT` check fails a thread slot that goes `-rtl_timeout` cycles without retiring an instruction.
Each Ariane or PicoRV32 tile has four thread slots, and three of them never retire, so a run fails with `TIMEOUT` once it lasts that many cycles.
Its cycle limit ends a run after `-max_cycle` cycles with verdict `maxcycles`.

Each loop simulation runs with `-rtl_timeout` set to the spec's `rtl_timeout`, or to 1,000,000 (`RECOMMENDED_RTL_TIMEOUT` in `mace/workloads.py`) when that is `None`.
OpenPiton's own default is 50,000 cycles.
When the spec sets `max_cycle`, each simulation also runs with `-max_cycle`; otherwise OpenPiton's testbench ends a Verilator simulation after 1,500,000 cycles.
A run stops at the lower limit, so an `rtl_timeout` above 1,500,000 needs a `max_cycle` as well.
No command-line flag sets either field: build the spec in Python, or set them on a task of the [evaluation suite](../06_mace_evaluation/harness.md).

## Budget

| Field | Default | Cap |
|---|---|---|
| `max_iterations` | `10` | Plan, execute, and triage cycles. |
| `max_usd` | `20.0` | Summed cost in USD of the run's planner, task, and triage calls. |
| `max_wall_s` | `3600` | Elapsed seconds since the run started. |

The caps are independent and must be positive.
The loop checks them before each iteration, so a started iteration always finishes.
A run that uses all its iterations without a pass ends with status `budget_exceeded`.
[LLM Backends](LLM_Backends.md) explains how each backend reports cost.

## Planner directives

The planner replies with footer lines: `TASK:` lines form the DAG, and `CACHES:` and `CONFIG_RTL:` lines change one task's build.

```text
TASK: <id> | deps=<comma-separated task ids, or empty> | kind=config|workload|unit_test|rtl | <short instruction>
CACHES: <task id> | <name>=<size>,<associativity> ...
CONFIG_RTL: <task id> | <FLAG1> <FLAG2> ...
```

Task LLM calls get no tools, except a testbench editor for `unit_test` tasks and an RTL editor for `rtl` tasks. An `rtl` task can change files under `piton/design/`, never the testbench or monitors under `piton/verif/`; see [Parallel Task Execution](../03_mace_design/task_execution.md). Otherwise only these directives change a task's build.
`CACHES:` accepts `l1i`, `l1d`, `l15`, and `l2`, with defaults `l1i=16384,4 l1d=8192,4 l15=8192,4 l2=65536,4`.

### CONFIG_RTL

`CONFIG_RTL:` adds RTL defines to one task's build as `-config_rtl=<FLAG>` arguments to `sims`.
They extend the default `MINIMAL_MONITORING` and never replace it.
Each flag must match `^[A-Z][A-Z0-9_]*$`.
The parser drops other tokens and merges several lines for one task.

- Nothing checks a define against the RTL, so an unknown define is accepted and costs a full build.
- A task also builds with the defines of every task it depends on, directly or through other tasks. `CACHES:` overrides stay with the task they name.
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
`result.post_mortem` holds the post-mortem of a run that ended `failed` or `budget_exceeded`.
[Results and Metrics](Results_and_Metrics.md) lists the statuses.
