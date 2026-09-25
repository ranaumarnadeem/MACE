% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Planner

`mace.planner.plan(spec, llm, tools=(), feedback="")` turns a `MaceSpec` into a task DAG with one LLM call.
It sends the prompt from `build_prompt()`, parses the response with `mace.agents.parse_tasks()`, and checks the result with `mace.integrator.topological_levels()`.

## Prompt

The prompt names the core, objective, target mesh, and gate workloads, and specifies the directive lines below.
On a replan, `build_prompt()` appends the triage feedback under the line `Feedback from a previous attempt, to inform this plan:` (see [Failure Analysis](failure_analysis.md)).

## Directive Lines

```text
TASK: <id> | deps=<comma-separated task ids, or empty> | kind=config|workload|unit_test | <short instruction>
CACHES: <task id> | <name>=<size>,<associativity> ...
CONFIG_RTL: <task id> | <FLAG1> <FLAG2> ...
```

For example:

```text
TASK: base | deps= | kind=workload | Run the gate workload on the default caches
TASK: l1d_8way | deps=base | kind=config | Rebuild with an 8-way L1D and run it again
CACHES: l1d_8way | l1d=8192,8
```

The parsers in `mace/agents.py` share three rules.
A tag starts its line, after optional whitespace and an optional list marker (`-`, `*`, `•`, `1.`, `1)`), and matches case-insensitively.
A value ends at the end of its line or before another known tag on the same line.
A malformed line is dropped without an error.
Triage and post-mortem directives follow the same rules, and each keeps its last match (see [Failure Analysis](failure_analysis.md)).

`TASK:` lines accumulate in response order.
Each needs four `|`-separated fields: a non-empty id, a `deps=` field, a `kind=` field naming a known kind, and the instruction.

`CACHES:` accepts `l1i`, `l1d`, `l15`, and `l2`, each with a positive integer size and associativity, and drops malformed entries.
Several lines for one task merge, and a later value for the same cache wins.
The prompt lists the defaults: `l1i=16384,4 l1d=8192,4 l15=8192,4 l2=65536,4`.

`CONFIG_RTL:` accepts upper-snake-case identifiers such as `CONFIG_DISABLE_BIST_CLEAR`.
The flags from all of a task's lines form a sorted union.
The flags add to the default RTL defines and never replace them.
No allowlist applies, so any well-formed name reaches the build.

An override line applies only to the task it names (see [Parallel Task Execution](task_execution.md)).

## Validation

`Task.__post_init__` in `mace/spec.py` rejects an empty id, a kind outside `TASK_KINDS`, an empty dependency id, an unknown or duplicated cache name, a non-positive cache size or associativity, and a malformed or duplicated `config_rtl` flag.
`parse_tasks()` drops any line whose `Task` fails these checks.
`plan()` raises `PlanningError` when no task remains, or when `topological_levels()` finds a duplicate id, a dependency on an unknown id, or a cycle.
The orchestrator then logs the error, which quotes a reply with no `TASK:` lines or names the graph problem, and ends the run with status `planning_failed`.

## Task Kinds

| Kind | Instruction | Execution |
|---|---|---|
| `config` | A configuration or RTL change | Build the task's configuration, run the first gate workload |
| `workload` | Running or fixing a gate workload | Same as `config` |
| `unit_test` | One module's RTL path relative to the checkout root, such as `piton/design/chip/tile/pico/rtl/picorv32.v` | Scaffold a unit-test environment, adapt its testbench, build it |

`config` and `workload` share one execution path; the label records intent.
