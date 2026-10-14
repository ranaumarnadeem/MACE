% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Directed Tests

The gate workloads are fixed programs that people wrote.
A `test` task adds checks of the loop's own: an agent writes a directed test program for the logic it suspects, and the loop runs the program on the built design.
A directed test adds a check and never replaces a gate workload.
`mace/directed.py` holds the flow, `mace.tools.DirectedTestTool` is the agent's tool, and [Directed-Test Evaluation](../06_mace_evaluation/directed_tests.md) describes how a test is judged.

## The test task

A `test` task builds the design as it stands, with the RTL edits of the tasks it depends on applied.
It asks its agent for up to three test programs, runs each on every tile of the build, and records the verdicts.
The task passes whatever the verdicts were: a test that fails on a design with a bug is a result, and the task that repairs the design is the one that must make it pass.
It also passes when no test ran to a verdict, with its attempts on record, so a failed test-writer never costs the run the iteration its repair task needs.
A design that does not build passes the task without a test, because nothing can run on it.
A build-only check (`LoopOptions(check="build")`) writes no tests.

A plan gets a `test` task only through a plan hook.
The planner's prompt does not offer the kind, and `mace.agents.parse_tasks` drops a task line that names it.
`mace.eval.directed.test_first()` is such a hook: it puts a `test` task before each `rtl` task, with the same dependencies and build overrides, and makes the `rtl` task depend on it.

The task has a form for two cores:

| Core | Form | File | Pass rule |
|---|---|---|---|
| PicoRV32 | RV32 assembly in the style of OpenPiton's `riscv/pico` tests | `dt_<name>.S` | `TEST_PASSFAIL` reaches the good trap on every hart |
| Ariane | C with `main(argc, argv)` | `dt_<name>.c` | `main` returns 0 on every hart |

On any other core the task fails and says that directed tests are not available.

The task's prompt gives the form's skeleton and the rules the harness learned from failed tests: define every label a test branches to, branch to labels and not to numeric offsets, and end an assembly test with `TEST_PASSFAIL`.
It also asks for boundary operands (0, 1, the largest and smallest signed values, the largest unsigned value, and a byte or halfword with its top bit set), because signed and unsigned handling and sign extension differ only at those values.

## The agent's tool

`DirectedTestTool` has four methods: `list`, `read`, `search`, and `write_test`.
The first three read the design under `piton/design/` and the example programs and headers under `piton/verif/diag/`, and nothing else.
`write_test(name, target, source)` saves one test.
The name is lowercase letters, digits, and underscores, starts with a letter, and takes at most 40 characters.
The target is the RTL file under `piton/design/` that the test is about, and it must exist.
The source must look like its form: an assembly test contains `RVTEST_CODE_BEGIN` and `TEST_PASSFAIL`, gives each `TEST_CASE` a different number, and has no `#` comment inside a `TEST_` macro, and a C test defines `main`.
A source takes at most 30,000 characters, and a task keeps at most three tests; writing a name again replaces its test.

The tool writes only `dt_<name>.<form>` and a `tests.json` sidecar of the task's tests, in one directory under the checkout's `build/.mace_tests/`.
It cannot write under `piton/`, and it cannot write to `mace/workloads/`, whose files are checksummed: a run ends `checksum_mismatch` if a program is added there.
A file under `build/` leaves the checkout's source fingerprint and every build ID unchanged.

## Attempts

An attempt is one agent call.
The loop reads the tests it wrote from the sidecar and runs each one with `-asm_diag_root` set to the attempt's directory.
OpenPiton's `sims` stops when one directory holds two diag programs of one name, so each attempt, and each set of inherited tests, has a directory of its own.

A test that ran to a verdict ends the attempts.
If no test ran, the next attempt gets the reason: that nothing was written, or the assembler or compiler log of each test that gave no verdict.
The task makes at most three attempts, and none starts after the run's time budget has passed.

OpenPiton's `rv32_as` script ignores the exit status of the compiler, so a PicoRV32 program that does not assemble still runs, with no image, and ends in a `timeout`.
A `timeout` or `maxcycles` run whose assembler or compiler log holds an error, such as an undefined label, therefore counts as `no_verdict`, and its log goes to the next attempt.

Each test is recorded with its attempt and one of these verdicts:

| Verdict | Meaning |
|---|---|
| `pass`, `fail`, `timeout`, `maxcycles` | The simulation reported it. |
| `no_verdict` | The test did not assemble or compile, or the simulation reported nothing. |
| `not_built` | The design did not build. |

## Tests in later tasks

The tests of a passed `test` task that ran to a verdict pass to the tasks that depend on it, directly or through other tasks, the way RTL edits do.
Such a task runs its gate workloads first, then the inherited tests, and passes only if every program passes.
The first program that fails ends the run of programs, so the failure evidence that triage and the next plan read is the failing test's log.
An `rtl` task's agent also sees each inherited test's source, its verdict on the design before the change, and the end of its log when it did not pass.
The prompt says a test is an agent's work and can be wrong.

## Records

`record_iteration` writes one `directed_tests` row per test and attempt: the task, attempt, name, target, form, source, verdict, seconds, and the end of the log of a run that did not pass.
The `test_checks` table holds the mutation check of a test, which [Directed-Test Evaluation](../06_mace_evaluation/directed_tests.md) describes.
The `tasks` row of an inheriting task lists its test programs and their verdicts in `programs`.

## What is left out

A `unit_test` task still stops at its build.
A testbench on OpenPiton's shared `test_infrstrct` harness stops under Verilator with the `Settle region did not converge` error, so none runs; [Environment Patches](../04_chia_openpiton/environment_patches.md) describes the cause.
Directed tests are programs for that reason.
