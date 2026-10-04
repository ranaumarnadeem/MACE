% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# MACE Requirements Specification

## Introduction

This specification states what MACE does.
MACE is an agentic system that plans, builds, and verifies multicore OpenPiton hardware on the CHIA framework and gates every change on a Verilator result.
Each requirement can be checked by a test or by inspection.
The [MACE Design Document](../03_mace_design/intro.md) explains how the implementation works.

Every requirement has an identifier of the form `REQ-<AREA>-<n>`.
The areas are `PLAN` (planning), `EXEC` (parallel task execution), `VER` (integration and verification), `FAIL` (failure analysis and replanning), `BUD` (budgets, workload checksums, and run status), `REPLAY` (deterministic replay), `PLAT` (platform), and `TOOL` (toolchain).
"Shall" marks mandatory behavior.
The terms objective, task, level, gate workload, verdict, iteration, and run ID have the meanings given in the design document's [Introduction](../03_mace_design/intro.md).

## Scope

MACE takes an English hardware objective and a run specification, plans the work as a task DAG with an LLM, and executes the tasks in parallel on OpenPiton checkouts.
This specification covers the loop in `mace/` and the chia_openpiton behavior it relies on: planning, task execution, integration, verification, failure analysis, budgets, workload checksums, run records, caching, and replay, plus the platform and toolchain they run on.
MACE builds and simulates with Verilator only.

A run targets one of the three cores that OpenPiton integrates and `chia_openpiton` configures: [Ariane](../05_mace_cores/ariane.md) (`ariane`), [OpenSPARC T1](../05_mace_cores/sparc.md) (`sparc`), and [PicoRV32](../05_mace_cores/pico.md) (`pico`).
The OpenPiton operations the loop uses (build, run, and, through the diagnostic tools, collect) belong to the [chia_openpiton Adapter](../04_chia_openpiton/intro.md).
The [MACE User Manual](../01_mace_user/Introduction.md) describes the command-line tools.

Two items fall outside this scope.
MACE does not write the L15 adapter a new core needs ([Adding a Core](../05_mace_cores/adding_a_core.md)).
Replay covers the return values of tagged calls and does not reproduce edits that agents make to a checkout.

## Functional Requirements

### Planning

- **REQ-PLAN-1:** MACE shall accept a run specification that names a core, a target mesh, one or more gate workloads, an objective, a budget, and a coverage flag.
- **REQ-PLAN-2:** MACE shall accept the cores `ariane`, `sparc`, and `pico`, with `ariane` as the default.
- **REQ-PLAN-3:** MACE shall accept a target mesh of 1 to 256 tiles on each axis, with 1x1 as the default.
- **REQ-PLAN-4:** MACE shall reject a run specification with an unknown core, a target mesh that is not a pair of integers from 1 to 256, an empty workload list, a workload name that is not a non-empty string, an empty objective, a non-boolean coverage flag, or a budget of the wrong type.
- **REQ-PLAN-5:** MACE shall produce each plan from one LLM call whose prompt states the core, the objective, the target mesh, and the gate workloads.
- **REQ-PLAN-6:** MACE shall read each task from a line of the form `TASK: <id> | deps=<ids> | kind=<kind> | <instruction>`.
- **REQ-PLAN-7:** MACE shall recognize a directive tag case-insensitively at the start of a line, including after a list marker such as `-`, `*`, or `1.`.
- **REQ-PLAN-8:** MACE shall accept the task kinds `config`, `workload`, and `unit_test`, and no others.
- **REQ-PLAN-9:** MACE shall skip a malformed task line and keep the well-formed ones.
- **REQ-PLAN-10:** MACE shall let a plan set the `l1i`, `l1d`, `l15`, and `l2` cache geometry of the task a line names, with lines of the form `CACHES: <task id> | <name>=<size>,<associativity> ...`, accepting only positive integers.
- **REQ-PLAN-11:** MACE shall let a plan add RTL defines to the task a line names, with lines of the form `CONFIG_RTL: <task id> | <FLAG> ...`, accepting only upper-snake-case identifiers.
- **REQ-PLAN-12:** MACE shall merge several `CACHES:` lines for one task, a later value for a cache replacing an earlier one, and shall merge several `CONFIG_RTL:` lines for one task into the union of their flags.
- **REQ-PLAN-13:** MACE shall end the run with status `planning_failed` when a plan yields no valid task, or when its tasks contain a duplicate id, a dependency on an undefined task, or a dependency cycle.
- **REQ-PLAN-14:** MACE shall include every earlier diagnosis in the run, with its suggested fix, in each later planning prompt.

### Parallel Task Execution

- **REQ-EXEC-1:** MACE shall group a plan's tasks into levels in which every task's dependencies lie in earlier levels.
- **REQ-EXEC-2:** MACE shall run at most one task on an OpenPiton checkout at a time, running each level in batches of at most one task per checkout.
- **REQ-EXEC-3:** MACE shall run the `config` and `workload` tasks of a batch concurrently.
- **REQ-EXEC-4:** MACE shall dispatch all remote builds and runs for one checkout to the same Ray worker.
- **REQ-EXEC-5:** MACE shall give every `config` and `workload` task its own build configuration, formed only from the run's core and mesh, the task's cache overrides, and the RTL defines of the task and of every task it depends on, added to the default defines.
- **REQ-EXEC-6:** When the run specification requests coverage, MACE shall build every `config` and `workload` task with Verilator line coverage.
- **REQ-EXEC-7:** MACE shall pass the tools the caller supplies to the loop to every LLM call it makes: planning, task execution, diagnosis, and post-mortem.
- **REQ-EXEC-8:** For a `unit_test` task, MACE shall scaffold an OpenPiton unit-test environment for the RTL module the task names, give the agent the module's port list, and ask it to reconcile the scaffolded testbench with the module.
- **REQ-EXEC-9:** When Ray is initialized, MACE shall give the agent of a `unit_test` task an edit tool limited to reading and rewriting that task's testbench file and reading the target module's source.
- **REQ-EXEC-10:** MACE shall report to an optional caller-supplied callback each `config` and `workload` task's entry into the prompting, building, and running stages, and the start of each `unit_test` task as building.
- **REQ-EXEC-11:** MACE shall build and run a `config` or `workload` task when its prompt call fails, and log the failure.
- **REQ-EXEC-12:** MACE shall fail a `unit_test` task, without scaffolding or building, when its module path is absolute, resolves outside the checkout, is not a `.v`, `.sv`, or `.pyv` file, or does not exist.
- **REQ-EXEC-13:** MACE shall fail an Ariane task whose L1D or L1I has more ways than its L1.5 without building it, and shall name the cache that breaks the rule in the failure.

### Integration and Verification

- **REQ-VER-1:** For each `config` and `workload` task, MACE shall build the task's configuration with Verilator and, when the build succeeds, simulate each gate workload of the run specification on it in order, stopping at the first whose verdict is not `pass`.
- **REQ-VER-2:** MACE shall take a simulation's verdict from the testbench transcript and ignore the simulator's exit code.
- **REQ-VER-3:** MACE shall classify each simulation from its transcript as `pass`, `fail`, `timeout`, or `maxcycles`, shall classify a simulation that exceeds its wall-clock limit without a transcript verdict as `timeout`, and shall otherwise leave it unclassified.
- **REQ-VER-4:** MACE shall not classify a transcript that contains a failure or max-cycles marker as `pass`, even when it also contains a PASS line.
- **REQ-VER-5:** MACE shall mark a `config` or `workload` task passed only when its build succeeds and every gate workload's verdict is `pass`, with no simulation process timing out or failing to launch.
- **REQ-VER-6:** MACE shall mark a `unit_test` task passed when its unit-test environment builds.
- **REQ-VER-7:** MACE shall require every tile of the mesh to reach the good trap for a simulation to pass.
- **REQ-VER-8:** MACE shall simulate gate workloads with an RTL timeout of 1,000,000 cycles, or with the run specification's `rtl_timeout` when it sets one.
- **REQ-VER-9:** MACE shall stop an iteration after the first level that contains a failed task.
- **REQ-VER-10:** MACE shall end the run with status `passed` when every task of an iteration passes.
- **REQ-VER-11:** MACE shall end the run with status `failed` when an iteration produces no task results.
- **REQ-VER-12:** MACE shall reuse an earlier successful build on the same checkout only when its core, mesh, network, cache geometry, RTL defines, and flags match, and the checkout's commit, file edits, untracked files, Ariane submodule, and Verilator version are unchanged since that build.
- **REQ-VER-13:** MACE shall record every run, iteration, and task result in a SQLite database, including the cache sizes and associativities and the RTL defines in each build's configuration.
- **REQ-VER-14:** MACE shall report each iteration's results to an optional caller-supplied callback after recording them and before failure analysis.
- **REQ-VER-15:** MACE shall report five metrics for a recorded run: successful tasks, iterations, failures recovered, execution time, and compute cost.
- **REQ-VER-16:** When the run specification sets `max_cycle`, MACE shall pass it to every gate workload's simulation as the testbench's cycle limit.

### Failure Analysis and Replanning

- **REQ-FAIL-1:** When an iteration contains a failed task, MACE shall diagnose the first failed task with one LLM call that asks for a diagnosis label and a suggested fix.
- **REQ-FAIL-2:** MACE shall give the diagnosis call the task's id, kind, instruction, build flags, build outcome, and verdict, plus the failure reason, error lines, and output tail of a failed build, or the simulation log tail and status log of a failed run.
- **REQ-FAIL-3:** MACE shall classify a failed `unit_test` build whose output contains `%Error-PINNOTFOUND` as `testbench_mismatch`, without an LLM call.
- **REQ-FAIL-4:** When a diagnosis response has no diagnosis line, MACE shall record the diagnosis `unknown` with the fix `retry with more context` and continue the run.
- **REQ-FAIL-5:** When Ray is initialized, MACE shall offer the diagnosis call and the post-mortem call tools that search the failed run's logs, collect text files from its run directory, compare its transcript with a reference transcript, and show the compiled program's symbols beside the run's symbol table.
- **REQ-FAIL-6:** The diagnostic tools MACE provides shall not write files, start a build, or start a simulation.
- **REQ-FAIL-7:** When the diagnostic tools cannot start, MACE shall continue the run without them.
- **REQ-FAIL-8:** MACE shall record each diagnosis with its run, iteration, and task.
- **REQ-FAIL-9:** When a run passes after one or more recorded failures, MACE shall mark every failure of that run as recovered.
- **REQ-FAIL-10:** MACE shall request a post-mortem only for a run that ends with status `failed` or `budget_exceeded` after at least one iteration. The request shall ask for one of `fixable_config`, `likely_hardware_limitation`, or `inconclusive`, and MACE shall record the assessment, explanation, and next steps it returns.
- **REQ-FAIL-11:** MACE shall discard a post-mortem response that has no assessment, without changing the run's status.

### Budgets, Checksums, and Run Status

- **REQ-BUD-1:** MACE shall limit each run by a maximum number of iterations, a maximum LLM cost in US dollars, and a maximum wall-clock time in seconds, with defaults of 10, 20.0, and 3600.
- **REQ-BUD-2:** MACE shall reject a budget with a limit that is not positive, an iteration or time limit that is not an integer, a cost limit that is not a number, or a boolean in any field.
- **REQ-BUD-3:** MACE shall check the wall-clock and cost limits before each iteration starts, and end the run with status `budget_exceeded` when either limit is exceeded.
- **REQ-BUD-4:** MACE shall end the run with status `budget_exceeded` when the maximum number of iterations completes without a pass.
- **REQ-BUD-5:** MACE shall count the reported cost of every planner, task, and triage LLM call toward the cost limit.
- **REQ-BUD-6:** MACE shall record the cost and wall-clock time of each iteration.
- **REQ-BUD-7:** Before any LLM call or checkout access, MACE shall verify the SHA-256 digest of every C program in `mace/workloads/` against `mace/workloads/CHECKSUMS`.
- **REQ-BUD-8:** MACE shall end the run with status `checksum_mismatch`, and run no iteration, when a digest differs or when the programs present differ from the programs `CHECKSUMS` lists.
- **REQ-BUD-9:** MACE shall record a run with status `running` when it starts, and replace that status with `passed`, `failed`, `planning_failed`, `budget_exceeded`, `checksum_mismatch`, or `error` when the loop ends.
- **REQ-BUD-10:** MACE shall record status `error` for a run that an exception ends, then re-raise the exception.
- **REQ-BUD-11:** MACE shall cap each build's and simulation's timeout at the time left in the run's wall-clock limit, and shall not start a build or simulation after that limit has passed.
- **REQ-BUD-12:** MACE shall record, for each LLM call, its phase, its input, output, and thinking tokens, its cost, its wall-clock time, whether it succeeded, and the text of its reply.

### Deterministic Replay

- **REQ-REPLAY-1:** MACE shall tag each remote prompt, build, and run call of a `config` or `workload` task with `<run ID>/iter<iteration>/<task id>/<phase>`, where the phase is `prompt`, `build`, `run` for the first gate workload, or `run:<workload>` for each later one.
- **REQ-REPLAY-2:** MACE shall let a caller enable caching, after which the return value of every tagged call to a function marked for caching is stored.
- **REQ-REPLAY-3:** MACE shall let a caller enable replay for named functions, after which their tagged calls marked for bypass are served from the cache without executing.
- **REQ-REPLAY-4:** A bypassed call whose tag has no cache entry shall fail with a cache-miss error instead of executing.

### Evaluation

- **REQ-EVAL-1:** The evaluation harness shall load its tasks from a suite file and reject a malformed entry, including an expert configuration that `PitonConfig` rejects, before any job starts.
- **REQ-EVAL-2:** The harness shall run each method on every suite task it applies to, for the requested number of repeats, in an order shuffled by a seed.
- **REQ-EVAL-3:** The harness shall skip a job whose run already ended with a final status other than `error`, so a stopped batch resumes.
- **REQ-EVAL-4:** Before each job, unless told to keep them, the harness shall remove every model directory whose name starts with `mace_` from each checkout.
- **REQ-EVAL-5:** Every baseline and co-design method shall check its designs through the loop's own build-and-check path, with the same timeouts and pass check as the loop.
- **REQ-EVAL-6:** Every LLM baseline and the co-design proposer shall state a task's inputs with the planner's own rendering of them.
- **REQ-EVAL-7:** After a passed run with a build-only check, the harness shall simulate each design the run accepted and record each gate workload's verdict.
- **REQ-EVAL-8:** A co-design search shall count a design feasible only when every gate workload passes on it and its cache area fits the task's budget, and shall record every design it evaluates.
- **REQ-EVAL-9:** The harness shall record with each run its method, suite task, repeat, seed, and environment: MACE's commit, each checkout's source fingerprint and Verilator version, the model, and the host.
- **REQ-EVAL-10:** A seeded-fault run shall change only its first plan's `config` and `workload` tasks, and shall record the fault's name with the run.
- **REQ-EVAL-11:** A fault check shall build and check the task's expert configuration with the fault applied, call no LLM, and record the fault's name with the run. A fault shall run only on tasks whose core and mesh it breaks.

## Platform Requirements

- **REQ-PLAT-1:** Every machine that builds or simulates, including the machine that runs the loop, shall run Linux and provide `bash`.
- **REQ-PLAT-2:** Every checkout path passed to the loop shall exist on the machine that runs the loop.
- **REQ-PLAT-3:** The Ray instance or cluster that runs MACE shall advertise one unit of the `openpiton` custom resource per OpenPiton checkout.
- **REQ-PLAT-4:** A worker that advertises N units of `openpiton` shall host N separate checkouts.
- **REQ-PLAT-5:** A worker that executes remote LLM prompt calls shall advertise the selected backend's credentials resource: `opencode_creds`, `claude_creds`, `antigravity_creds`, or `vertex_creds`.
- **REQ-PLAT-6:** Every OpenPiton checkout shall be patched with `scripts/patch_openpiton.sh` before its first build.
- **REQ-PLAT-7:** MACE shall support the CHIA LLM backends `opencode`, `claude`, `antigravity`, and `vertex`, and no others, selected by an explicit argument or else by the `MACE_LLM` environment variable, which defaults to `vertex`. The `mace` CLI and `examples/mace_end_to_end.py` shall select `vertex` unless `--backend` names another.
- **REQ-PLAT-8:** MACE shall take the model name from an explicit argument, or else from the `MACE_LLM_MODEL` environment variable, and shall use `gemini-2.5-flash` for the `vertex` backend when neither gives one. The `mace` CLI and `examples/mace_end_to_end.py` shall use `gemini-2.5-flash` when the `vertex` backend is selected and `--model` is not given, even when `MACE_LLM_MODEL` is set.
- **REQ-PLAT-9:** The `chia_openpiton` package shall import nothing from `mace`, so that it can be installed in CHIA as `chia/openpiton/` on its own.

## Toolchain Requirements

| Component | Version | Defined in |
|---|---|---|
| Python | 3.10 | `pyproject.toml` (`>=3.10,<3.11`) |
| Ray | `>=2.54,<3` | `pyproject.toml` |
| typer, rich | `>=0.12`, `>=13` | `pyproject.toml` |
| pytest | `9.0.3` | `pyproject.toml` (`test` extra) |
| CHIA (`chialoops`) | 1.0.1 (tag `v1.0.1`) | `flake.nix`, the installation instructions |
| Verilator | 5.052 pinned; the evaluation used 5.020 | `flake.nix` (`nixpkgs-verilator` input), `cluster/local.yaml` |
| RISC-V GCC | `riscv64-elf-ubuntu-24.04-gcc`, release 2026.08.27 | `flake.nix`, `cluster/local.yaml` |
| OpenPiton | commit `1c6bfd2` | `cluster/local.yaml` |

- **REQ-TOOL-1:** MACE shall run on Python 3.10.
- **REQ-TOOL-2:** MACE shall install with `typer>=0.12`, `rich>=13`, and `ray>=2.54,<3`, and its tests shall run with `pytest==9.0.3`.
- **REQ-TOOL-3:** MACE shall run on CHIA's v1.0.1 release, installed from PyPI as `chialoops==1.0.1` or from a clone of the `v1.0.1` tag.
- **REQ-TOOL-4:** Builds shall use a Verilator release that builds this RTL: 5.052, which `flake.nix` pins and `cluster/local.yaml` builds, or 5.020, which the evaluation used, both with `--no-timing`.
- **REQ-TOOL-5:** Workers shall provide a `riscv64-unknown-elf` GCC that covers `rv64imafdc`/`lp64d` for Ariane diags and the `rv32ima`/`ilp32` multilib for PicoRV32 diags.
- **REQ-TOOL-6:** Every machine that patches, scaffolds, or builds an OpenPiton checkout shall provide `python3`, and workers that build Ariane shall also provide `dtc` for the boot ROM.
- **REQ-TOOL-7:** Workers shall provide `objdump` on `PATH` for the `symbol_check` diagnostic tool.
- **REQ-TOOL-8:** Each OpenPiton checkout shall be at commit `1c6bfd2`, with the `piton/design/chip/tile/ariane` submodule initialized.
- **REQ-TOOL-9:** `nix develop` shall provide Python 3.10 with pip and virtualenv, Verilator 5.052, the RISC-V GCC, and the system packages OpenPiton's scripts use, among them gawk, GNU make, bison, flex, tcsh, dtc, Perl with `Bit::Vector`, and libelf. On first entry it shall install CHIA's v1.0.1 release and MACE, editable with its test and eval extras, into its virtual environment, and on later entries install only a package that environment lacks.
