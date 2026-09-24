% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Failure Analysis

When an iteration holds a failed task, the orchestrator diagnoses the first failed result with `mace.triage.triage()` and feeds the diagnosis into the next plan.
Other failed tasks in that level get a row in the `tasks` table and no triage.

## Triage

`triage(result, llm, tools=())` makes one LLM call.
The prompt gives the task's id, kind, and instruction, the build outcome, and the run verdict.
A failed build adds the failure reason and the last 1500 characters of stderr.
A failed run adds the last 1500 characters of the simulation log and up to 8000 characters of the status log.
The prompt asks for two directive lines:

```text
DIAGNOSIS: <a short label, e.g. test_bug, config_error, timeout, maxcycles, rtl_suspect, testbench_mismatch>
FIX: <a short, concrete instruction for what to try next>
```

`parse_diagnosis()` returns the last `DIAGNOSIS:` value, lowercased, and `parse_fix()` the last `FIX:` value; a missing fix is recorded as empty.
`KNOWN_DIAGNOSES` lists the six labels, but any label is recorded.
Without a `DIAGNOSIS:` line, triage raises `TriageError`, and the orchestrator records `unknown` with the fix `retry with more context`.

A failed `unit_test` build whose stderr contains `%Error-PINNOTFOUND` skips the LLM call and gets `testbench_mismatch`, with a fix aimed at the testbench's port connections rather than the module.

## Diagnostic Tools

When Ray is initialized, the orchestrator starts a `PitonToolServer` named `triage-<run ID>` on the first checkout for each triage.
It stops the previous server first and hands the new one the failed task's build and run.
It exposes four read-only tools:

| Tool | Returns |
|---|---|
| `grep` | Regex matches from the run's `sim.log`, `status.log`, or `fake_uart.log` |
| `collect` | Small text files from the run directory, by glob |
| `compare_to_fixture` | The first divergence between `sim.log` and a reference transcript in `chia_openpiton/test/fixtures/` |
| `symbol_check` | `objdump -f` and `-t` output for the run's `diag.exe`, beside its `symbol.tbl` |

A task that failed at build has no run, and the tools report an error for it.
Triage gets the caller's tools plus this server.
If the server fails to start, the run goes on without it.
The last server also serves the post-mortem and stops when the run ends.
See [Tool Server](../04_chia_openpiton/tool_server.md).

## Replanning

`record_failure()` writes the diagnosis to the `failures` table, and the orchestrator adds a line to the run's feedback history:

```text
Task <id> (<instruction>) failed: diagnosis=<label>, suggested fix=<fix>
```

Every later `plan()` call receives the whole history.
If the run then passes, `mark_all_recovered()` marks all of its failures recovered.
A replanned DAG can use new task ids, so recovery is tracked per run.

A tier-1 test, `mace/test/cluster/orchestrator_e2e_test.py`, exercises the detect, diagnose, and replan cycle with a stub `sims` that fails its first run and passes after that.
No passing 2x2 or 4x4 run has needed the cycle.

## Post-Mortem

A run that ends with `failed` or `budget_exceeded` after at least one iteration gets one more LLM call, `mace.report.generate_post_mortem()`.
Its prompt holds the objective, core, mesh, stop reason, and one line per task tried, with the diagnosis on the triaged task.
The prompt asks for three directive lines:

```text
ASSESSMENT: <fixable_config|likely_hardware_limitation|inconclusive>
EXPLANATION: <the specific evidence that led to this assessment>
NEXT_STEPS: <what to try next, or why nothing more is worth trying>
```

`record_post_mortem()` stores the result in the `post_mortems` table.
A response without `ASSESSMENT:` is dropped, and the run status stands.
