% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# RTL-Parameter Searches

OpenPiton's `sims` sets cache sizes, associativities, the mesh, and the network. Its buffer depths, queue sizes, and miss-handling counts sit in RTL headers and packages, where `sims` cannot reach them. An RTL-parameter search changes those values by editing the source, builds each edited design, and keeps a design only when every tile passes and the finish time drops.

The searches run on `matmul_scaled.c`, a program whose work grows with the hart count and which reports how long each hart took. MACE's proposer reads those per-hart timings before it picks the next design. A TPE sampler from Optuna searches the same parameters with the same number of simulations.

## The program

`mace/workloads/matmul_scaled.c` gives every hart four rows of a matrix product against the same 56x56 matrix. A 4x4 mesh does four times the work of a 2x2 mesh, and a mesh with no contention finishes both at the same time. The second matrix is 12.25 KB, which is larger than the default 8 KB L1D, so it does not stay in the L1D, and the harts read the same lines from the L2 slices.

Each hart stores the elements it computes on its own stack, reads them back, and checks each one against a closed form. A hart that finds a wrong value ends at the bad trap, so the run passes only when every tile is right. Each hart reads `mcycle` before its first row and after its last, then prints one line in hart order:

```text
h3 1204 188731
```

`chia_openpiton.parse.hart_cycles()` reads these lines from `fake_uart.log` (see [Transcript Parsers](../04_chia_openpiton/parsers.md)). The values are core clock cycles. The finish time is the testbench's simulation time when the last hart ends, so the two are in different units, and a hart is compared with the other harts and not with the finish time. Harts are numbered row by row, so hart `h` is on tile `(h mod X, h div X)` of an `X` by `Y` mesh.

Printing to the fake UART takes many cycles per character, and the harts print one after another. The line is short for that reason, and the finish time includes the printing. The cycle counts leave it out, so the scaling study uses them, and the finish time is reported next to them.

## The parameters

Each parameter lists the places its value is written. A search sets the value of every parameter in its space, and a parameter left out keeps its default.

| Parameter | Default | Values | Storage bits per unit | Controls |
|---|---|---|---|---|
| `l2_mshr` | 8 | 4, 8, 16 | 154 | Entries in each L2 slice's miss status table |
| `l2_req_buf` | 8 | 4, 8, 16 | 64 | Header flits an L2 slice holds for requests from the L1.5 caches |
| `l2_req_data_buf` | 4 | 2, 4, 8 | 64 | Data flits an L2 slice holds for those requests |
| `dcache_wbuf` | 8 | 4, 8, 16 | 147 | Entries in each Ariane core's data cache write buffer |
| `store_buffer` | 4 | 2, 4, 8 | 262 | Entries in each Ariane core's speculative and committed store queues |
| `scoreboard` | 8 | 4, 8, 16 | 362 | Instructions an Ariane core can have in flight |
| `home_alloc` | MIXED | LOW, MIDDLE, HIGH, MIXED | 0 | Which address bits choose the L2 slice that homes a line |

`mace/codesign/rtl.py` holds the catalog. The storage cost of a design is the flip-flop bits it adds per tile over the default design. The bits per unit come from the entry widths in the RTL: for example, a store buffer entry holds a 56-bit address, 64 bits of data, an 8-bit byte enable, a 2-bit size, and a valid bit, and the value sets two queues. A smaller buffer gives bits back. The cost orders designs by size and is not a synthesis result.

Some parameters are left out. `NR_LOAD_PIPE_REGS` and the floating-point latencies trade instructions per cycle for clock period, which a simulation cannot see. The L1.5 thread count, which sets how many stores the data cache keeps in flight, is tied to masks written into several L1.5 files. The L1.5 request queue and the router buffers need changes in several files that must agree, and are left for later.

## How an edit works

A parameter lists one or more places that hold its value, each as a line of text with the value in it. The text at the default value must occur exactly once in its file. If it does not, the design is rejected with the reason, and nothing is built. A checkout that differs from the one the catalog was written against fails there and never builds the wrong design.

Edits go to the `.pyv` sources and never to the `.tmp.h` and `.tmp.v` files that PyHP writes from them. Each build overwrites those. A value that a header and a PyHP block both hold is written in both places. After a build, the search checks that the generated L2 header holds the new text. A header that was not regenerated would build the default design and look like a parameter with no effect, so a build without the edit is rejected and does not count as a simulation.

The edits are applied through `OpenPitonWorkspaceNode.apply_edits`, set into `PitonConfig.source_edits` so that each distinct design builds into its own model directory, and reverted after the runs however they end. A design met again reuses its build. The searches keep the build cache between jobs, so the default design builds once.

## The storage budget

A task's `storage_budget_ratio` is a multiple of the default design's storage in the searched parameters, as `area_budget_ratio` is for the cache co-design. The budget is the ratio's excess over 1 times that storage, rounded down, as flip-flop bits per tile that a design may add. At the ratio of 1.0 that the task file sets, a design may not add storage, so a larger buffer must be paid for with a smaller one. The ratio is fixed before any search runs, so the budget does not shape the result.

A design over the budget is rejected before it builds and costs no simulation. The budget keeps a search from taking the largest value of every buffer. Every method on a task gets the same budget, except `rtlparam_default` and `rtlparam_sweep`, which have none: they measure what each value does and are not searches.

## Methods

| Method | What it does | LLM | Repeats |
|---|---|---|---|
| `rtlparam_default` | Builds and simulates the default design only, for its finish time and per-hart cycles | No | Once |
| `rtlparam_sweep` | The default design, then each parameter alone at each of its other values | No | Once |
| `rtlparam_trades` | The default design, then each design under the task's `trades`, which fit the storage budget | No | Once |
| `rtlparam_mace` | The default design, then one design per round from MACE's proposer | Yes | Per repeat |
| `rtlparam_bayes` | The default design, then designs from Optuna's TPE sampler, seeded by the repeat | No | Per repeat |

Every search starts with the default design, so each result has a baseline from the same run. The proposer makes no LLM call for that first design. For each later design it sees the parameters with their defaults, values, and costs, the storage budget, every design tried with its outcome, and the per-hart cycles of the best design and the latest one. It replies with a `BOTTLENECK:` line, one sentence on what limits the finish time, and a `DESIGN:` line with the values to try. The bottleneck line is recorded with the design.

TPE starts modelling after three random trials, where Optuna's default is ten. With ten simulations, the default would leave the sampler nothing to model.

## Trades

At a ratio of 1.0, no single increase fits the budget, so the sweep cannot show an in-budget gain by itself. A design can win only by trading: a larger buffer paid for with a smaller one. `examples/rtlparam_pick_trades.py` reads a task's latest sweep, takes the effect of each passing single change on the finish time, and ranks the combinations that fit the budget by the sum of their effects.

The sum assumes the effects add. It chooses which designs to simulate and is not a result. The script prints a `trades:` list to paste into the task's `rtlparam` block. The loader refuses a trade that is the default design, a repeat, outside the space, or over the budget. `rtlparam_trades` then simulates the default design and each trade, and only those simulations show whether a trade beats the default.

```bash
python examples/rtlparam_pick_trades.py --task ariane-4x4-matmul-scaled --db runs/rtlparam.db --k 5
```

## Counting simulations

A search stops after its number of simulations, after ten times that many proposals, when its time budget runs out, or when its strategy proposes nothing new twice in a row. A design counts as a simulation when it builds and its gate programs run, whether or not it passes. A proposal rejected before a build costs nothing: it was over the storage budget, an edit did not apply, or the build lacked the edit. Reports compare searches by simulations, never by proposals.

## Running a batch

```bash
MAKEFLAGS=-j1 python examples/eval_batch.py --suite examples/eval/tasks_rtlparam.yaml \
  --piton-root ~/openpiton-eval --tasks ariane-4x4-matmul-scaled --repeats 3 \
  --methods rtlparam_default,rtlparam_sweep,rtlparam_mace,rtlparam_bayes \
  --backend vertex --db-path runs/rtlparam.db
```

`examples/eval/tasks_rtlparam.yaml` has one task per mesh: 1x1, 2x2, 4x2, and 4x4 Ariane. Each task's `rtlparam` block gives the values each searched parameter may take, the number of simulations per search, and the storage budget ratio. A parameter left out of the space keeps its default, and the values must include the default. The searches use one checkout.

```yaml
rtlparam:
  simulations: 10
  storage_budget_ratio: 1.0
  space:
    l2_mshr: [4, 8, 16]
    dcache_wbuf: [4, 8, 16]
```

## What a run records

A search is one run in the metrics database, labelled with its method and task. It has one `evaluations` row per proposal:

| Column | Holds |
|---|---|
| `design` | The value of every parameter, as JSON |
| `sim_time` | The summed finish time of the gate programs, or empty for a design that failed or was rejected |
| `feasible` | 1 for a design that passed |
| `simulated` | 0 for a design rejected before any simulation |
| `rejected` | The reason, for a design rejected |
| `storage_bits` | The bits the design adds per tile over the default design |
| `hart_cycles` | Each gate program's per-hart `[start, end]` values, as JSON |
| `note` | The proposer's bottleneck line |

Each design that built also has a task row. Its `rtl_edits` column holds the diff of the files the edits changed.
