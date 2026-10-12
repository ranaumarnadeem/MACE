% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Fixed Checks

Four checks make the loop stronger without another LLM call.
Each is a `LoopOptions` field that is on by default, and an ablation method turns it off (see [Evaluation Harness](../06_mace_evaluation/harness.md)).

| Check | Field | Module | What it does |
|---|---|---|---|
| Plan check | `plan_check` | `mace/plan_check.py` | Rejects a plan that cannot work before any build |
| Raw evidence | `raw_evidence` | `mace/triage.py` | Gives the re-plan the build's own report beside the diagnosis |
| Narrowing | `narrowing` | `mace/narrowing.py` | Rebuilds a failed task with its changes undone to find which one breaks it |
| Fast path | `fast_path` | `mace/fast_path.py` | Runs a plan that builds one design as a single task, without the per-task LLM calls |

## Plan Check

`_plan_checked()` in `mace/orchestrator.py` makes the iteration's plan: one `plan()` call, then a look at the result before any build.
The look has two parts.

`strip_unknown_defines()` removes each RTL define that no file mentions.
`define_index()` collects the upper-snake-case words of the text files under `piton/design`, `piton/verif`, `piton/tools/src`, and `piton/tools/verilator` in the first checkout, once per process.
A define in none of them changes nothing, so a plan that carries one only builds a second model that behaves like the first.
A define the objective names stays.

`check_plan()` then returns one `PlanProblem` per reason the plan cannot work.
It builds nothing and calls no LLM.

| Rule | A task is rejected when |
|---|---|
| `source_edit` | In a run without RTL edits, it has kind `rtl`, or its `config` or `workload` instruction asks to edit a Verilog or SystemVerilog file |
| `required_define` | It builds without an RTL define that a sentence of the objective names as needed |
| `way_rule` | Its caches break [Ariane's way rule](../04_chia_openpiton/piton_config.md) |
| `missing_module` | It is a `unit_test` of a path that is not in the checkout |
| `repeated_failure` | It builds the same flags as a configuration that already failed in the run, a failed probe included |
| `known_bad_change` | It makes every change of a set that narrowing showed breaks the design |

A task that builds on `rtl` edits skips the last two rules, since its flags do not show its edits.
The `missing_module` rule needs a checkout, and the other rules need only the plan and the run's history.

A rejected plan goes back to `plan()` with the reasons in its `rejection` argument, as a call in the `plan_retry` phase.
An iteration sends its plan back at most `MAX_PLAN_REJECTIONS` times, which is 2.
A plan still rejected after that runs as it stands, and so does the rejected plan when the retry gives no usable plan.
The seeded-fault `plan_hook` runs after the check, so a fault always reaches a build.

`record_plan_check()` writes one row of the `plan_checks` table for each look that found a problem or removed a define, and for each retry's look.
`action` is `rejected`, `accepted`, or `kept`, and the row holds the rules, tasks, and reasons as JSON, and the stripped defines.
`mace.eval.plan_replay.replay_plans()` runs the same check over the plans recorded in a database, with no LLM call and no build.

## Raw Evidence

The note a failed task adds to the feedback holds four parts:

```text
Task <id> (<instruction>) failed (build succeeded: <b>, run verdict: <v>).
Changes from the defaults: <changes>
What the build and simulation reported: <digest>
Triage, an LLM's reading of this evidence: diagnosis=<label>, suggested fix=<fix>
```

`evidence_digest()` writes the digest in about 800 characters: the build's error lines, or the failing program's verdict, its first status line, and the end of its simulation log.
`collapse_similar_lines()` turns a run of lines that differ only in their numbers into the first line and a count.
Triage's own prompt keeps the longer text of `failure_evidence()`.
`changes_from_defaults()` also names a default define that the build left out.

With `raw_evidence=False`, the note is the changes, the label, and the fix on one line.

## Narrowing

A failed task often carries several changes from the defaults, and the build is deterministic, so a rebuild answers which change breaks it.
Before it triages a failed `config` or `workload` task, `narrow()` in `mace/narrowing.py` rebuilds the task with some of its changes undone.

The reference is the mesh's default configuration plus the RTL defines the objective says the build needs.
A `Change` is a difference between the failed configuration and the reference, in either direction:

| Kind | The failed configuration | Undoing it |
|---|---|---|
| `added_define` | Has a define the reference lacks | Removes the define |
| `missing_define` | Lacks a default or required define | Adds the define |
| `cache` | Sets a cache off its default geometry | Restores the default |
| `network` | Uses the crossbar | Restores the mesh interconnect |

A probe is the failed task with some changes undone, built and checked on the same gate workloads through `integrate_parallel()`, on the run's open checkouts, with task prompts off.
Up to four changes are probed one at a time, as many probes together as there are checkouts, and then all are undone in one probe if none passed.
A task with more changes goes straight to the all-undone probe.
A probe starts only while twice the failed task's own build and run time remains, and at least 300 seconds, so the re-plan that follows still fits.

| Verdict | Meaning | Triage |
|---|---|---|
| `culprit` | Undoing a change makes the task pass | Skipped; the failure is recorded as `config_change` with a fix that names the change |
| `combination` | Undoing any one change leaves the task failing, but undoing all of them passes | Skipped; the fix is the default configuration |
| `not_config` | The reference fails too | Runs, and its prompt carries the probes' note |
| `unresolved` | The run's time ran out first | Runs, and its prompt carries the note |
| `no_changes`, `skipped` | Nothing to undo, or the failure is not a configuration's (`way_rule`, a build that never started, RTL edits, another kind of task) | Runs |

A passing probe does not count as the run's pass, because it may leave out a cache size the objective asked for.
The re-plan decides what to build next, with the note in its feedback.
A failing probe joins the configurations the plan check refuses to build again, and the changes of a `culprit` or `combination` join `known_bad_change`.
`record_probes()` writes each probe to the `probes` table, apart from the `tasks` table, so the counts of a plan's tasks and failures stay as they were.

With `narrowing=False`, or with `triage="raw"` or `"off"`, no probe runs.

## Fast Path

When all the tasks of a plan resolve to the same build flags, they are one design with several names, and each task after the first reuses the build and simulates the same programs again.
`collapse_single_design()` keeps the last task in dependency order, with no dependencies, when every task is a `config` or `workload` task and `config_key()` gives one set of flags.
The orchestrator then runs that task with `task_prompts` off, so the run makes no LLM call of its own for its tasks, since their replies change nothing that is built.
`record_fast_path()` writes the kept task and the ids it replaced to the `fast_paths` table.

A plan with more than one design keeps all its tasks and their calls.
The collapse follows the seeded-fault `plan_hook`, so a fault applies to the task that runs.
With `fast_path=False`, every plan runs as the planner wrote it.
