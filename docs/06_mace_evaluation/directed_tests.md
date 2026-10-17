% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Directed-Test Evaluation

[Directed Tests](../03_mace_design/directed_tests.md) describes how an agent writes test programs for the logic it suspects.
This page describes how the harness judges them.
`mace/eval/directed.py` holds the code, and the planted bugs of the [harness](harness.md) supply the faults.

Two questions have an answer in the records:

1. Detection: for a planted bug, does MACE write a test that fails on the bug and passes on the correct design?
2. Repair: does writing a test first help `mace_rtl` repair the bug?

## The mutation check

A generated test counts only if it passes on the correct design and fails on the planted bug in the logic it targets.
`check_tests` decides that for every test a run wrote.
It runs each test on the task's expert design twice: with the task's `source_fault` applied, and after `restore()` has removed the fault.
Each test runs alone from a directory of its own, and each design is built once for all the tests.
The check calls no LLM and runs after the method has finished, so its time is not part of the iterations' wall time or the method's budget.

A null test runs beside the generated tests on both designs: a program that checks nothing (`TEST_PASSFAIL` alone for PicoRV32, `return 0` for Ariane).
A bug that fails even the null test fails every program, so a generated test that fails on it has not found the bug.
The Ariane bug below is such a bug: an atomic add that subtracts hangs the C runtime's exit barrier, which every program reaches.

| Outcome | Verdict with the bug | Verdict without the bug | Null test with the bug |
|---|---|---|---|
| `detected` | `fail`, `timeout`, or `maxcycles` | `pass` | `pass` |
| `runtime_broken` | `fail`, `timeout`, or `maxcycles` | `pass` | not `pass` |
| `missed` | `pass` | `pass` | any |
| `false_alarm` | any | `fail`, `timeout`, or `maxcycles` | any |
| `invalid` | no verdict | `pass` | any |
| `unchecked` | `not_built` | `pass` | any |

A hang counts as a failure, unless the log shows that the program did not assemble: that is no verdict, and the design page says why.
A test with no verdict, or no build, on the correct design is also `invalid`, and so is every test of a run whose null test fails on the correct design.
The `unchecked` outcome is for a bug that breaks the build, such as the three bugs of `examples/eval/tasks_rtl.yaml` that sit above the held-out block: no test can run on a design that does not build, so only the false-alarm rate of the tests written for them is defined.
Each outcome is a row in the `test_checks` table, keyed like the test's row in `directed_tests`; the null test's row has the task `null` and the outcome `null`, and the reports leave it out of the test counts.

## Methods

| Method | What runs |
|---|---|
| `expert_clean` | The expert configuration without the task's `source_fault`, built and checked once with no LLM. It passes when the task's gate workload passes on a correct design. |
| `testgen` | Detection. The expert design with the bug is built and its gate workloads run (iteration 0). One `test` task then writes tests for it, told what the gate workloads reported (iteration 1). The run ends `passed` when the mutation check finds a detected test, and `failed` otherwise. It uses one checkout. |
| `mace_rtl_defs` | Repair, the control of `mace_rtl_test`. `mace_rtl` with a plan hook that adds the RTL defines the task's objective says its design needs to every task of every plan. |
| `mace_rtl_test` | Repair. `mace_rtl_defs` with a second plan hook that puts a `test` task before each `rtl` task. The mutation check then judges every test the run wrote. |
| `mace_rtl`, `retry_agent_rtl` | The existing methods that may edit RTL, run on the same tasks for comparison. |

`expert_clean`, `testgen`, `mace_rtl_defs`, and `mace_rtl_test` run only on tasks that have a `source_fault`.
`expert` on the same task is the check that the bug makes the gate workload fail.
On a bug that breaks the build, `testgen` builds the design, fails, and writes no test, so the evaluation runs it on the held-out bugs only.
The hook of `mace_rtl_test` adds a test only before an `rtl` task, so a plan with no `rtl` task has no test.

The defines hook exists because the planner names a PicoRV32 task's required define, `CONFIG_DISABLE_BIST_CLEAR`, for its first workload task and leaves it out of its `rtl` tasks.
Every gate run of such a repair then times out, whatever the edit was: this happened in all four laptop pilot runs of the loop that reached a repair iteration, in October 2026.
The [plan check](../03_mace_design/fixed_checks.md) now adds the objective's required defines to every task that builds, `rtl` tasks included, so with it on `mace_rtl` gets the define too and the hook adds nothing more.
`mace_rtl_defs` against `mace_rtl_test` stays the comparison that isolates the tests.
On a design that needs no define, such as Ariane's, the hook changes nothing and the two repair methods differ only in the tests.

## The held-out bugs

The three bugs of the first block of `examples/eval/tasks_rtl.yaml` break the build.
Every test fails on a design that does not build, so they cannot tell a test that targets the logic from one that does not.
The block at the end of that file holds four bugs that build and fail in simulation.
They were defined on 2026-10-12, on logic that a gate workload or a reasonable directed test exercises.
The first three were defined before any prompt of the `test` task was written; the fourth after the first version of that prompt, which was tuned on the development bug only:

| Task | File | Change | Gate workload |
|---|---|---|---|
| `ariane-2x2-barrier-amobug` | `l2_amo_alu.v` | An atomic add subtracts. | `barrier_atomic.c` |
| `pico-2x2-sh-rtlbug` | `picorv32.v` | `lh` zero-extends in place of sign-extending. | `sh.S` |
| `pico-2x2-srai-rtlbug` | `picorv32.v` | The four-bit step of the arithmetic right shift fills with zeros. | `srai.S` |
| `pico-2x2-mulhsu-rtlbug` | `picorv32.v` | `mulhsu` treats its second operand as signed. | `mulhsu.S` |

The Ariane bug is a repair task: its null test fails on it, so the detection tables count its tests as `runtime_broken`.
The three PicoRV32 bugs are the detection tasks.

`examples/eval/tasks_tests.yaml` holds a fourth bug, a PicoRV32 `bgeu` that compares signed values, on which the prompts of the `test` task were tuned.
No evaluation reports it.

A held-out task is `verified: true` once `expert` on it builds and then fails in simulation, and `expert_clean` on it passes.

## Running it

```bash
export MAKEFLAGS=-j1
python examples/eval_batch.py --piton-root ~/openpiton --suite examples/eval/tasks_rtl.yaml --include-unverified \
    --tasks ariane-2x2-barrier-amobug,pico-2x2-sh-rtlbug,pico-2x2-srai-rtlbug,pico-2x2-mulhsu-rtlbug \
    --methods expert,expert_clean --db-path runs/heldout_check.db
python examples/eval_batch.py --piton-root ~/openpiton --piton-root-2 ~/openpiton-2 --backend vertex \
    --suite examples/eval/tasks_rtl.yaml --include-unverified --repeats 5 \
    --methods testgen,mace_rtl,mace_rtl_defs,mace_rtl_test,retry_agent_rtl --db-path runs/directed.db
```

The first command needs no LLM.
`retry_agent_rtl` and `mace_rtl` run on every task of the suite; `testgen`, `mace_rtl_defs`, and `mace_rtl_test` run on the tasks that have a fault.
