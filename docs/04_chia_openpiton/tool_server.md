% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# PitonToolServer

`PitonToolServer`, in `chia_openpiton/tools.py`, is the MCP tool server an LLM agent uses to drive one OpenPiton checkout. It subclasses CHIA's `AsyncJobTool` and calls [OpenPitonWorkspaceNode](workspace_node.md) members in-process, following the convention of CHIA's `Gem5ToolServer`. Construction binds the operational state: the checkout root, the timeouts, and the diag search directory. The model chooses only what to run.

## Construction

```python
PitonToolServer(
    name: str,
    piton_root: str,
    config: PitonConfig,
    *,
    asm_diag_root: str | None = None,
    build_timeout_s: int = 7200,
    run_timeout_s: int = 3600,
    expose: tuple[str, ...] | None = None,
    task_options: dict | None = None,
    last_build: PitonBuildArtifact | None = None,
    last_run: PitonRunResult | None = None,
)
```

`piton_root` must be an existing local directory, and `config` is the starting [PitonConfig](piton_config.md). Every run receives `asm_diag_root`. Construction starts the MCP server in a Ray actor, and builds and runs execute in that actor. Pass the workspace node's `node.task_options` as `task_options` to place it on the worker that holds the checkout. `last_run` points the inspection tools at a run produced elsewhere, as described under Inspecting an earlier run below. No tool reads `last_build`.

## Tools

```python
build(clean: bool = False) -> dict
run(test: str, precompiled: bool = False, finish_mask: str | None = None,
    rtl_timeout: int | None = None, max_cycle: int | None = None) -> dict
job_status(wait_seconds: int = 30) -> dict
grep(source: str, pattern: str, context: int = 3, max_lines: int = 40) -> str
collect(pattern: str = "*", max_bytes: int = 200_000) -> str
config_get() -> str
config_set(x_tiles: int | None = None, y_tiles: int | None = None, core: str | None = None,
           network_config: str | None = None, config_rtl: list[str] | None = None,
           extra_flags: list[str] | None = None) -> str
compare_to_fixture(fixture_name: str, max_context: int = 5) -> str
symbol_check() -> str
```

| Tool | Behaviour |
|---|---|
| `build` | Starts a build of the current configuration as a background job. A configuration that already built successfully on the unchanged checkout is served from the build cache; `clean=True` forces a rebuild |
| `run` | Starts a run of `test` against the current configuration's built model. `finish_mask` defaults to one digit per tile; `rtl_timeout` and `max_cycle` reach `sims` as `-rtl_timeout=` and `-max_cycle=` |
| `job_status` | Waits up to `wait_seconds`, capped at 120, for the current job and reports its state |
| `grep` | Searches one log of the last run with a Python regex and returns each match with `context` lines around it, capped at `max_lines`. `source` is `"sim_log"` (`sim.log`), `"status_log"` (`status.log`), or `"fake_uart"` (`fake_uart.log`, the program's console output) |
| `collect` | Returns the text of the files in the last run's directory that match `pattern`; `**` is recursive. Files over `max_bytes` are listed with their size instead |
| `config_get` | Renders the current configuration: core, mesh, network, `config_rtl`, caches, `extra_flags`, and `build_id` |
| `config_set` | Changes the given fields and keeps the rest, including the caches, except `sys`, which returns to `"manycore"`. Re-runs `configure()` against the checkout, so the next build uses the recomputed `build_id`. Returns `OK, config updated:` and the new rendering |
| `compare_to_fixture` | Compares the last run's `sim.log` line by line against a captured transcript in `chia_openpiton/test/fixtures/`, such as `run_pass_sim.log`, using [first_divergence](parsers.md). Reports `identical to <fixture> through all <n> shared line(s)`, or the line number where the texts diverge, with that line and up to `max_context` lines before it from both texts. `fixture_name` must name a file directly inside that directory |
| `symbol_check` | Returns `objdump -f` and `objdump -t` output for the last run's `diag.exe`, followed by the run's `symbol.tbl` |

`symbol_check` returns raw data and leaves the matching to the model, because `good_trap` and `bad_trap` in `symbol.tbl` are not symbols in the binary. The model matches them by address; for example, the `good_trap` address appears as `pass` in the objdump symbol table.

## Build and run jobs

A Verilator build or an RTL simulation takes minutes, so `build` and `run` return at once with `{"started": True, "running": True}`. Build and run share one job slot. While a job runs, another `build` or `run` call returns `{"started": False, "running": True, "note": ...}`. `job_status` returns `{"done": False, "running": True, ...}` until the job finishes, then `{"done": True, "running": False}` merged with the job's result:

| Job | Result keys |
|---|---|
| `build` | `job_type="build"`, `success`, `returncode`, `failure_reason`, `reused`, `wall_time_s`, `binary_path` |
| `run` | `job_type="run"`, `success`, `returncode`, `verdict`, `sim_time`, `wall_time_s` |

Before any job has started, `job_status` returns `{"done": False, "running": False, "note": "nothing started yet"}`. `build` takes a snapshot of the configuration when called, so a later `config_set` does not change a running build.

## Selecting tools with expose

`expose=None` registers all nine tools. A tuple registers only the named ones. For example, `("config_get", "config_set")` suits an agent that edits configuration and hands building to another component. An unknown name raises `ValueError`, listing the valid names, before the server starts.

## Inspecting an earlier run

`grep`, `collect`, `compare_to_fixture`, and `symbol_check` read the last run's directory. To inspect a run produced elsewhere, such as by another `OpenPitonWorkspaceNode`, pass it as `last_run` at construction. The server answers from a copy of the object made at construction, so later assignments to the caller's object never reach it. To inspect a different run, start a new server. MACE's orchestrator starts one such read-only server for each triage (condensed from `mace/orchestrator.py`; see [Failure Analysis](../03_mace_design/failure_analysis.md)):

```python
tool_server = PitonToolServer(
    f"triage-{run_id}", piton_roots[0], PitonConfig(),
    expose=("grep", "collect", "compare_to_fixture", "symbol_check"),
    last_build=failed.build, last_run=failed.run,
)
```

## Tool names

Each tool registers under the MCP name `f"{name}_{tool}"`, so a server named `piton` exposes `piton_build`, `piton_run`, `piton_job_status`, and so on. CHIA's API model backends present each function to the model as `f"{tool.name}__{fn.name}"[:64]`, for example `piton__piton_build`. A long `name` can make two function names collide after the truncation to 64 characters.

## Failure behaviour

The inspection tools return strings and do not raise on bad input. Errors start with `ERROR: `:

| Condition | Returned text |
|---|---|
| No run yet | `ERROR: no run yet; call <name>_run(...) first` |
| Unknown `grep` source | `ERROR: source must be one of ['fake_uart', 'sim_log', 'status_log'], got '<source>'` |
| Invalid regex | `ERROR: bad regex '<pattern>': <reason>` |
| Unknown fixture | `ERROR: unknown fixture '<name>'; available: [<.log files>]` |
| `objdump` not installed | `ERROR: 'objdump' was not found on PATH` |
| `objdump` failed | `ERROR: 'objdump -f <binary>' failed (exit <n>): <stderr>`, or the same for `-t` |

Missing data comes back as a note in parentheses, such as `(no lines match '<pattern>')`, `(no sim.log in this run)`, or `(no diag.exe in this run directory)`. `config_set` does not catch validation errors: an invalid value raises the `ValueError` from `PitonConfig`.

A failed build or run shows up in `job_status` with `success=False`. So does an exception from `OpenPitonWorkspaceNode.build()` or `run()`, such as the `ValueError` that `build()` raises when another `configure()` call has changed the checkout since the configuration was made (see [OpenPitonWorkspaceNode](workspace_node.md)). The job's result then has only `job_type`, `success=False`, and `error`, which holds the exception's type and message:

```python
{"done": True, "running": False, "job_type": "build", "success": False,
 "error": "ValueError: checkout at '<piton_root>' no longer matches build_id '<build_id>''s recorded file edits ..."}
```

The job logs the traceback at ERROR level through the `chia_openpiton.tools` logger. The inspection tools keep reading the previous run.
