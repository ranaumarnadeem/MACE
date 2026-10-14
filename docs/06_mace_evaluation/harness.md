% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Evaluation Harness

The evaluation harness runs every method on every task of a suite, records each run in one database, and turns that database into tables.
`mace/eval/` holds it, and `examples/eval_batch.py` and `examples/eval_report.py` drive it.
[Methodology](methodology.md) describes the smaller comparison behind the Table 1 results.

## Task suite

`examples/eval/tasks.yaml` lists the tasks, and `mace.eval.suite.load_suite()` reads and checks it.
`examples/eval/tasks_sparc.yaml` holds the OpenSPARC T1 tasks, which need Verilator 5.052; run them with `--suite` and a `VERILATOR_ROOT` that points at 5.052.
Each task names a core, a mesh, the gate workloads, the objective every method gets, a budget, and optional `rtl_timeout` and `max_cycle` limits for every simulation; [Running the Loop](../01_mace_user/Running_the_Loop.md) describes both.
An optional `workload_args` block maps a program to its own `finish_mask`, a quoted string of hex digits, and extra `run_args`.
Its `expert` block holds the known passing configuration: cache overrides and extra RTL defines on top of the defaults.

`verified: true` marks a task whose expert configuration has passed.
Besides the four Table 1 cells, the verified bring-up tasks cover other meshes, MACE's own gate programs, OpenPiton's Ariane C tests, and PicoRV32 ISA tests, and the co-design task is verified because its default caches pass `matmul.c`.
OpenPiton's Ariane C tests that poll, with plain loads, a counter other harts update atomically, such as `hello_world_many.c`, pass only on a checkout with fix 13, which [Ariane (CVA6)](../05_mace_cores/ariane.md) describes.
Every hart runs the program, so a test that checks one hart's view of shared memory, such as `amo_align.c` or `amoadd_w.S`, cannot pass on a mesh and is left out.
The batch runner skips a candidate unless it gets `--include-unverified` or names the task in `--tasks`.

Loading checks every field and builds each expert configuration as a `PitonConfig`, so a malformed entry fails before any job starts.

## Methods

| Method | Label | What runs |
|---|---|---|
| `mace` | MACE | The full loop. |
| `expert` | B0, expert reference | The expert configuration, built and checked once through the loop's own build-and-check path, with no LLM. It sets a floor on machine time. |
| `one_shot` | B1, one-shot | The loop with a one-iteration budget, no triage, and no post-mortem, so its plan comes from the planner's own prompt. Its runs also serve as the no-re-plan ablation. |
| `retry_agent` | B2, retry agent | One agent proposes one design per attempt on one checkout; after a failure it gets the failure's raw evidence, the text triage would have read. |
| `no_triage` | A1 | The loop with `LoopOptions(triage="raw")`. |
| `one_checkout` | A2 | The loop on the first checkout only. |
| `build_check` | A3 | The loop with `LoopOptions(check="build")`. After a passed run, the harness simulates each design it accepted and records the verdicts in the `resimulations` table; a design that fails is a false accept. |
| `no_reuse` | A5 | The loop with `LoopOptions(reuse_builds=False)`. |
| `mace_rtl` | Opt-in | The loop with `LoopOptions(rtl_edits=True)`: its planner may offer `rtl` tasks, whose agents edit the design's RTL. Meant for source-fault tasks. |
| `retry_agent_rtl` | Opt-in | `retry_agent` whose design may be an `rtl` task, with the loop's edit tool. |
| `one_shot_rtl` | Opt-in | `one_shot` with `LoopOptions(rtl_edits=True)`: one plan, run once, which may hold `rtl` tasks. |
| `codesign_mace` | C0 | A co-design search whose designs come from the LLM proposer. |
| `codesign_random` | C1 | A co-design search over uniform random designs, seeded by the repeat. |
| `codesign_grid` | C2 | A co-design search over the task's fixed grid, in order. |
| `codesign_bayes` | C3 | A co-design search with Optuna's TPE sampler, seeded by the repeat. |
| `seeded_<fault>` | RQ3 | The loop with its first plan broken by one seeded fault; see below. |
| `seeded_retry_<fault>` | RQ3, B2 | The retry agent with its first design broken by one seeded fault, on one checkout; see below. |
| `faultcheck_<fault>` | Fault check | The expert configuration with one seeded fault applied, with no LLM; it should fail. See below. |
| `no_<part>` | Ablation | The loop with one of the four [fixed checks](../03_mace_design/fixed_checks.md) off: `no_plan_check`, `no_raw_evidence`, `no_narrowing`, or `no_fast_path`. |
| `seeded_no_<part>_<fault>` | RQ3, ablation | `seeded_<fault>` with one of those checks off. |

The co-design methods run only on tasks with a `codesign` block, and the others only on tasks without one.
`expert`, `codesign_grid`, and the fault checks have no randomness, so they run once per task unless `--expert-repeats` asks for more.

The LLM methods state a task's inputs with the planner's own `render_inputs()` and ask for cache and define overrides with its `OVERRIDE_RULES`, so a comparison differs in what each method does, not in what it is told.
`mace/baselines/` holds `expert`, `one_shot`, and `retry_agent`.

## Batch runs

`mace.eval.runner.plan_jobs()` expands the suite into one job per task, method, and repeat, and shuffles the jobs with `--shuffle-seed`, so a slow hour on Vertex does not fall on one method.
`run_batch()` skips each job whose run in the database already ended `passed`, `failed`, `planning_failed`, `budget_exceeded`, or `checksum_mismatch`, so a stopped batch resumes where it left off; a run left `running` or ended `error` runs again.

Before each job, `clear_build_cache()` removes every directory under `build/manycore/` whose name starts with `mace_` on every checkout, so each run starts from an empty build cache.
Models built by hand or by other tools stay.
On a checkout whose cached models you want to keep, pass `--keep-cache`.

Each run records its method, task, and repeat in the `runs` table, and the environment in `runs.meta`: MACE's commit and whether the tree had uncommitted changes, each checkout's source fingerprint and Verilator version, the backend, the model, the host, and `MAKEFLAGS`.
`--no-task-prompts` turns off each task's own LLM call in `mace`, `one_shot`, and the ablations, and the choice is recorded in `runs.meta`.
A batch whose methods are all among `expert`, the `faultcheck_<fault>` methods, `codesign_random`, `codesign_grid`, and `codesign_bayes` calls no LLM, so it builds no backend, needs no GCP project, and records the backend and model as null.

```bash
python examples/eval_batch.py --piton-root ~/openpiton --dry-run
MAKEFLAGS=-j1 python examples/eval_batch.py --piton-root ~/openpiton --methods expert,codesign_random,codesign_grid,codesign_bayes
export GOOGLE_CLOUD_PROJECT=<your-gcp-project> MAKEFLAGS=-j1
python examples/eval_batch.py --piton-root ~/openpiton --piton-root-2 ~/openpiton-b --repeats 3
python examples/eval_batch.py --piton-root ~/openpiton --methods no_triage,one_checkout,build_check,no_reuse \
    --tasks ariane-2x2-barrier,pico-2x2-addi
```

The dry run lists each job as `done` or `todo` and starts no Ray instance.

## Seeded faults

`mace/eval/faults.py` names the seeded faults.
Each changes the first plan's `config` and `workload` tasks before they run and leaves later plans alone, through `run_mace_loop`'s `plan_hook`.

| Fault | Change | Failure | Verified on |
|---|---|---|---|
| `fpga_synth` | Adds `PITON_FPGA_SYNTH` | The Verilator build fails with `%Error-PINNOTFOUND` | Ariane, PicoRV32 |
| `drop_bist` | Removes `CONFIG_DISABLE_BIST_CLEAR` | The PicoRV32 simulation times out | PicoRV32 |
| `crossbar` | Selects `xbar_config` | On a mesh with two or more rows, the Verilator build fails with duplicate pin connections | Ariane, PicoRV32 |
| `l1d_eight_way` | Sets an 8 KB eight-way L1D | `build()` refuses it under [Ariane's way rule](../04_chia_openpiton/piton_config.md), with failure reason `way_rule`, before any build | Ariane |
| `l15_eight_way` | Sets an 8 KB eight-way L1.5 | The way rule allows it and the build passes; the Ariane simulation times out, and a PicoRV32 mesh passes | Ariane |
| `no_mem_ctrl` | Adds `PITONSYS_NO_MC` | The build passes and the simulation times out | Ariane, PicoRV32 |
| `l2_direct_mapped` | Sets a one-way L2 | The Verilator build fails with a zero replication value and a missing module, `l2_priority_encoder_0` | Ariane, PicoRV32 |
| `drop_monitoring` | Removes `MINIMAL_MONITORING` | The PicoRV32 simulation ends with no pass status, and an Ariane mesh passes | PicoRV32 |

The last four faults are held out: they were defined after the loop's fixed checks were designed, with kinds the first four do not cover, and `Fault.held_out` lets a report treat the two groups apart.
Their fault checks ran on `ariane-2x2-barrier` and `pico-2x2-addi`, so they are verified on 2x2 meshes.
A fault that removes a default define sets `Task.drop_rtl`, which `_config_for_task()` honours; the planner has no directive for it, since `CONFIG_RTL:` only adds defines.

Three faults make two changes at once, each a single fault above that breaks the design alone, so undoing either one leaves it broken:

| Fault | Parts | Failure |
|---|---|---|
| `synth_and_no_mem` | `fpga_synth`, `no_mem_ctrl` | The build fails on the first; once it is undone, the simulation times out on the second |
| `bist_and_synth` | `drop_bist`, `fpga_synth` | The build fails on the second; once it is undone, the PicoRV32 simulation times out on the first |
| `l2_and_no_mem` | `l2_direct_mapped`, `no_mem_ctrl` | The build fails on the first; once it is undone, the simulation times out on the second |

`Fault.parts` names the single faults of such a fault, and a report groups them apart from the single-change faults.
Their fault checks ran on `ariane-2x2-barrier` and `pico-2x2-addi` (`bist_and_synth` on PicoRV32 only) and failed the build on the first part in every run; the second part's failure is its single fault's own.
A loop that reads one failure at a time needs a build or simulation per change, while narrowing's `combination` verdict undoes both together.
With the default budget of three iterations, a method that finds one cause per iteration has no attempt to spare.

A fault applies to bring-up tasks on the cores it lists, and `crossbar` only to meshes with two or more rows, since OpenPiton's crossbar has one port per column.
A `seeded_<fault>` method runs the loop with that fault seeded, only when a batch names it; `examples/recovery_seeded.py` runs one fault on one task.
A `seeded_retry_<fault>` method runs the retry agent with the same fault applied to its first design, through `run_retry_agent`'s `design_hook`, and leaves later designs alone; it applies to the same tasks as `seeded_<fault>`.
A `faultcheck_<fault>` method builds and checks the task's expert configuration with the fault applied, with no LLM, once per task; the fault breaks that task when the run fails.
A candidate fault becomes verified when its fault checks fail on every task it applies to, and one whose check passes is dropped.

## Plan-check replay

`mace.eval.plan_replay.replay_plans(db_path, checkout)` checks every planner reply recorded in a database as the loop would have, with no LLM call and no build.
It pairs each plan's problems with what happened when the plan ran, so a rule can be tried on earlier runs before a machine spends hours on it.
A plan that has a problem and passed is a rule that would have sent back a working plan.
The database opens read-only and immutable, because `DBReader` adds tables and columns to an older database.
`tally()` counts plans by rule and outcome.

```python
from mace.eval.plan_replay import replay_plans, tally

print(tally(replay_plans("runs/eval.db", checkout="/home/me/openpiton")))
```

## Reverted fixes

A reverted-fix run needs a checkout that lacks one fix of `scripts/patch_openpiton.sh`: patch a fresh checkout with `PATCH_SKIP` naming that fix (see [Environment Patches](../04_chia_openpiton/environment_patches.md)), and pass `--label reverted_fix=<N>` so each run records it.
Give each fix its own database: the runner skips a job whose task, method, and repeat already finished in the database, whatever the checkout.
Run `expert` on the checkout as well: it fails when the task still needs the fix.

```bash
PATCH_SKIP="11" bash scripts/patch_openpiton.sh ~/openpiton-no11
python examples/eval_batch.py --piton-root ~/openpiton-no11 --methods expert,mace --tasks ariane-2x2-barrier \
    --label reverted_fix=11 --db-path runs/reverted_fix_11.db
```

## Source faults

A suite task with a `source_fault` plants a bug in the design: text replacements in files under `piton/design/`, each an `old` snippet that occurs once and its `new` text (see `mace/eval/source_faults.py`).
Every job on the task runs on checkouts that carry the fault.
`faulted()` writes it into each checkout before the job and restores the files after, however the job ends, and a backup an interrupted job left behind is restored before the next fault is applied.
The backup sits apart from the RTL edit tool's, so an `rtl` task's edits and the fault never undo each other.
The run's `meta` lists the files the fault changed.

`examples/eval/tasks_rtl.yaml` holds three such tasks, two on Ariane and one on PicoRV32, each a misspelled or undeclared name that makes the Verilator build fail with an error that names the file and line.
The `ariane-2x2-accu-rtlfault` task was added after the `rtl` prompts were tuned on the other two, so no prompt change has seen it.
Only a method that can edit RTL can pass: `mace_rtl`, `retry_agent_rtl`, and `one_shot_rtl`.
`one_shot_rtl` sees no build before its one plan, and the planner plans an `rtl` task only after a failure names the file, so it is expected to fail.
`mace`, `one_shot`, and `retry_agent` change only the build configuration and are the controls.
The `expert` reference fails by design on these tasks, and its failure is the check that the fault breaks the build.
A task passes when its gate workload passes, so a fault must sit on a signal the workload needs: a first PicoRV32 fault, a misspelled wire read only by the optional coprocessor interface, was repaired by declaring the wire, and the workload still passed.
All three tasks passed the check on the laptop, each build stopping at the planted line:

| Task | File and line | Error |
|---|---|---|
| `ariane-2x2-hello-rtlfault` | `wt_l15_adapter.sv:255` | `Member 'l15_inval_icache_invalid' not found in structure` |
| `pico-2x2-addi-rtlfault` | `picorv32.v:391` | `Can't find definition of variable: 'mem_readyx'` |
| `ariane-2x2-accu-rtlfault` | `wt_dcache_ctrl.sv:84` | `Can't find definition of variable: 'address_offs_q'` |

## Co-design searches

A co-design task asks for the design that finishes its gate workloads soonest within a cache-area budget.
Its `codesign` block sets the search space, the grid for the grid search, the number of simulations per search, the batch size, and the area budget as a multiple of the area of OpenPiton's default caches.

A design sets each cache's size and associativity and the interconnect, `2dmesh_config` or `xbar_config`.
OpenPiton's crossbar has one port per column, so the suite loader accepts `xbar_config` only for a mesh with one row of tiles.
The space lists the allowed sizes and associativities of the searched caches; the others keep their defaults.
Each round, the strategy proposes up to `batch` designs it has not tried.
A design's cache area is known before it is built, and so is whether it keeps [Ariane's way rule](../04_chia_openpiton/piton_config.md): neither the L1D nor the L1I may have more ways than the L1.5.
`build()` would refuse a design that breaks the rule, so a search rejects one first.
A design over the budget or against the rule is recorded as infeasible, with the reason, without a build, and does not use up a simulation; the strategy hears the outcome like any other.
The rest build and run in parallel through the loop's own build-and-check path.
A design that passes has a finish time, the sum of its gate workloads' simulated finish times (`sim_time`), and is feasible.
A search stops after its simulations, after ten times that many proposals, when `max_wall_s` runs out, or when its strategy proposes nothing new twice in a row, and it ends `passed` when it found at least one feasible design.
Each proposal gets an `evaluations` row with its geometry, verdict, finish time, area, whether it was simulated, and why it was rejected before a build, if it was.
The LLM proposer's prompt also carries each cache geometry's area over all tiles, so it can add up a design's area before proposing it, and states the way rule on Ariane.
The suite loader rejects a grid that holds a design against the rule.

`mace.codesign.area` models each cache as a data array with one line per row and a tag array with one set per row and one tag per way, so the tag array widens with associativity.
A tag holds the 40-bit physical address above the set index and line offset, plus two state bits.
Lines are 16 bytes for L1I, L1D, and L1.5 and 64 bytes for L2, and the technology is 32 nm.
With a CACTI 7 binary named by `MACE_CACTI` or found as `cacti` on `PATH`, each array goes through CHIA's `run_cacti()`; otherwise through CHIA's `analytical_area_estimate()`, which charges one square micron per bit.
The two give different scales, so every design in a comparison should come from one of them; each `evaluations` row names its source.

The Bayesian search needs Optuna, which the `eval` extra installs: `pip install -e ".[eval]"`.
A design that fails the check, or breaks the budget, scores ten times the slowest feasible finish seen so far.

## Reports

```bash
python examples/eval_report.py runs/eval.db
python examples/eval_report.py vm1.db vm2.db --out runs/eval.md --csv runs/eval.csv
```

The report reads each database with `DBReader` and starts no Ray instance.
Within one database, a run that a later run of the same task, method, and repeat replaced, such as one a stopped batch left `running`, is left out of the tables, and the report gives their number.
For bring-up methods it writes one row per task and method, with passes out of runs, the median and interquartile range of time to pass, and medians of machine time, builds, simulations, LLM calls, thinking tokens, and dollars, and for `build_check` the false accepts.
Time to pass is the wall time of passed runs, and machine time sums build and simulation time.
A second table gives each method's tasks, runs, and passes, its median time to pass and machine time, and its total dollars across tasks.
A third compares each method with `mace` task by task, over the tasks where both passed at least once, with an exact two-sided Wilcoxon signed-rank test on time to pass.

A run made with `--model` set to a model other than `gemini-2.5-flash` appears as `<method>@<model>`, such as `one_shot@gemini-3.8-flash`, so each model gets its own rows.
Give each model's batch its own `--db-path`: the batch runner skips a job whose task, method, and repeat already finished in its database, whatever model ran it.

For co-design searches it writes one row per task and method: the median best feasible finish, the best finish any search of the task found, how many searches came within 5% of it and the median simulations they needed, the share of feasible designs, and how many proposals were rejected before a build.
`--csv` writes every run's numbers for plots, with `replaced` marking the runs the tables leave out.
