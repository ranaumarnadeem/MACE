% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Evaluation Harness

The evaluation harness runs every method on every task of a suite, records each run in one database, and turns that database into tables.
`mace/eval/` holds it, and `examples/eval_batch.py` and `examples/eval_report.py` drive it.
[Methodology](methodology.md) describes the smaller comparison behind the Table 1 results.

## Task suite

`examples/eval/tasks.yaml` lists the tasks, and `mace.eval.suite.load_suite()` reads and checks it.
Each task names a core, a mesh, the gate workloads, the objective every method gets, a budget, and an optional `rtl_timeout`.
Its `expert` block holds the known passing configuration: cache overrides and extra RTL defines on top of the defaults.

`verified: true` marks a task whose expert configuration has passed.
The four Table 1 cells are verified, and the co-design task is verified because its default caches pass `matmul.c`.
The other bring-up tasks are candidates: other meshes, the Ariane C tests in the checkout, a PicoRV32 ISA test, and an 8x8 PicoRV32 mesh.
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
| `codesign_mace` | C0 | A co-design search whose designs come from the LLM proposer. |
| `codesign_random` | C1 | A co-design search over uniform random designs, seeded by the repeat. |
| `codesign_grid` | C2 | A co-design search over the task's fixed grid, in order. |
| `codesign_bayes` | C3 | A co-design search with Optuna's TPE sampler, seeded by the repeat. |
| `seeded_<fault>` | RQ3 | The loop with its first plan broken by one seeded fault; see below. |

The co-design methods run only on tasks with a `codesign` block, and the others only on tasks without one.
`expert` and `codesign_grid` have no randomness, so they run once per task unless `--expert-repeats` asks for more.

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

```bash
python examples/eval_batch.py --piton-root ~/openpiton --dry-run
export GOOGLE_CLOUD_PROJECT=<your-gcp-project> MAKEFLAGS=-j1
python examples/eval_batch.py --piton-root ~/openpiton --piton-root-2 ~/openpiton-b --repeats 3
python examples/eval_batch.py --piton-root ~/openpiton --methods no_triage,one_checkout,build_check,no_reuse \
    --tasks ariane-2x2-barrier,pico-2x2-addi
```

The dry run lists each job as `done` or `todo` and starts no Ray instance.

## Seeded faults and reverted fixes

`mace/eval/faults.py` names the seeded faults.
Each changes the first plan's `config` and `workload` tasks before they run and leaves later plans alone, through `run_mace_loop`'s `plan_hook`.
`fpga_synth` adds `PITON_FPGA_SYNTH`, and the Verilator build fails; `drop_bist` removes `CONFIG_DISABLE_BIST_CLEAR`, and a PicoRV32 simulation times out.
Both broke every hackathon run they were seeded into and are marked verified.
`l1d_three_way` and `l15_below_l1d` are unverified candidates until a pilot shows they break a build or a simulation.
A `seeded_<fault>` method runs on bring-up tasks whose core the fault lists, and only when a batch names it; `examples/recovery_seeded.py` runs one fault on one task.

A reverted-fix run needs a checkout that lacks one fix of `scripts/patch_openpiton.sh`: patch a fresh checkout with `PATCH_SKIP` naming that fix (see [Environment Patches](../04_chia_openpiton/environment_patches.md)), and pass `--label reverted_fix=<N>` so each run records it.

```bash
PATCH_SKIP="11" bash scripts/patch_openpiton.sh ~/openpiton-no11
python examples/eval_batch.py --piton-root ~/openpiton-no11 --methods mace --tasks ariane-2x2-barrier     --label reverted_fix=11 --db-path runs/reverted_fix.db
```

## Co-design searches

A co-design task asks for the design that finishes its gate workloads soonest within a cache-area budget.
Its `codesign` block sets the search space, the grid for the grid search, the number of simulations per search, the batch size, and the area budget as a multiple of the area of OpenPiton's default caches.

A design sets each cache's size and associativity and the interconnect, `2dmesh_config` or `xbar_config`.
The space lists the allowed sizes and associativities of the searched caches; the others keep their defaults.
Each round, the strategy proposes up to `batch` designs it has not tried, and they build and run in parallel through the loop's own build-and-check path.
A design passes when every gate workload passes, and its finish time is the sum of their simulated finish times (`sim_time`).
It is feasible when it passes and its cache area fits the budget.
A search stops after its simulations, when `max_wall_s` runs out, or when its strategy proposes nothing new twice in a row, and it ends `passed` when it found at least one feasible design.
Each design gets an `evaluations` row with its geometry, verdict, finish time, and area.

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
For bring-up methods it writes one row per task and method, with passes out of runs, the median and interquartile range of time to pass, and medians of machine time, builds, simulations, LLM calls, thinking tokens, and dollars.
Time to pass is the wall time of passed runs, and machine time sums build and simulation time.
A second table sums each method across tasks.
A third compares each method with `mace` task by task, over the tasks where both passed at least once, with an exact two-sided Wilcoxon signed-rank test on time to pass.

For co-design searches it writes one row per task and method: the median best feasible finish, the best finish any search of the task found, how many searches came within 5% of it and the median simulations they needed, and the share of feasible designs.
`--csv` writes every run's numbers for plots.
