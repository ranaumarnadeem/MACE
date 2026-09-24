% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Parallel Task Execution

`mace.integrator.integrate_parallel()` executes a task DAG across the OpenPiton checkouts listed in `piton_roots`.

## Checkouts and Placement

OpenPiton's pyHP preprocessor writes generated `.tmp.v` files into the source tree on every build, so two builds in one checkout corrupt each other.
MACE therefore gives each concurrent task its own checkout.

`open_nodes()` constructs one `OpenPitonWorkspaceNode` per checkout concurrently, each waiting up to 120 s for its placement group.
If any construction fails, it closes the nodes that succeeded and raises.
Each node reserves a one-bundle Ray placement group of `{"CPU": 1, "openpiton": 1}`, and each of its member functions requests `{"openpiton": 1}`.
All calls through one node therefore land on the same worker.
A worker that advertises `{"openpiton": 2}` must host two separate checkouts.
See [Workspace Node](../04_chia_openpiton/workspace_node.md).

## Dispatch

Levels run in order (see [Integration and Verification](integrator.md)).
A level with more tasks than checkouts runs in batches of `len(piton_roots)`, one after another.
The i-th task of a batch runs on the i-th checkout.

Within a batch, each `config` or `workload` task runs on its own thread as three CHIA remote calls, each resolved with `get()` before the next:

- `llm.prompt.chia_remote()` with the task's instruction and the caller's tools;
- `node.build.chia_remote()` with the task's configuration;
- `node.run.chia_remote()` with the first gate workload, if the build succeeded.

Each call carries a replay tag (see [Budgets and Replay](budget_and_replay.md)).
Tasks in a batch advance through these stages independently.
The optional `on_task_progress(task_ids, stage)` callback reports `prompting`, `building`, and `running`.

A `unit_test` task skips this pipeline and runs locally through `mace.loop.run_mace_step()` on its slot's checkout.
A batch's `unit_test` tasks run one after another before its remote tasks start.

## Per-Task Configuration

`mace.loop._config_for_task(spec, task)` builds each task's `PitonConfig`:

- `core`, `x_tiles`, and `y_tiles` come from the spec;
- `extra_flags` holds `-vlt_build_args=--coverage-line` when `spec.coverage` is set;
- `caches` takes the task's `CACHES:` override, and sims applies its defaults to the caches the override omits;
- `config_rtl` is the sorted union of the default `("MINIMAL_MONITORING",)` and the task's `CONFIG_RTL:` flags.

A task's configuration depends only on the spec and the task's own overrides.
Two tasks in one level can therefore build different cache geometries.

## Unit-Test Tasks

`_run_unit_test_step()` reads `task.spec` as an RTL path.
It scaffolds an environment named by `unit_test_env_name()` with OpenPiton's `create_env.py`, reads the module's ports, and prompts the agent to fix the scaffolded `<env>_top.v`.
It then builds `PitonConfig(sys=env_name)`.

When Ray is initialized, the step creates a `mace.tools.TestbenchEditTool` for the LLM call and stops it afterward.
The tool exposes `{name}_read_testbench`, `{name}_write_testbench`, and `{name}_read_dut_source`, and none of them takes a path.
The agent can rewrite that testbench and read that module's source, and no other file.

`_unit_test_tool_name(task.id)` gives it a fixed-length name:

```python
f"ut_edit_{hashlib.sha1(task_id.encode()).hexdigest()[:8]}"
```

CHIA's API backends truncate each function name, `{tool}__{tool}_{method}`, to 64 characters.
The fixed 16-character name keeps the three function names distinct for any task id.
