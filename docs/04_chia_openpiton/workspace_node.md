% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# OpenPitonWorkspaceNode

`OpenPitonWorkspaceNode`, in `chia_openpiton/openpiton_workspace.py`, drives one OpenPiton checkout on one worker. Its members wrap `sims`.

```python
from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

with OpenPitonWorkspaceNode("/work/openpiton") as node:
    cfg = get(node.configure.chia_remote(x_tiles=1, y_tiles=1))
    art = get(node.build.chia_remote(cfg))
    res = get(node.run.chia_remote(cfg, "hello_world.c"))
    assert res.success
```

## Construction

```python
OpenPitonWorkspaceNode(piton_root: str, *, root_on_remote_worker: bool = False, **kwargs)
```

`piton_root` must name an existing local directory. With `root_on_remote_worker=True`, the node accepts an absolute path that exists only on a remote worker. That mode requires `require_colocated=False`, and the caller schedules each call. The other keyword arguments go to CHIA's `ColocatedNode`:

| Argument | Default | Effect |
|---|---|---|
| `placement_group` | `None` | Pin members to an existing placement group |
| `require_colocated` | `True` | Reserve a one-bundle placement group of `{"CPU": 1, "openpiton": 1}`; `False` leaves placement to the caller |
| `bundle_index` | `0` | Bundle to pin to |
| `reserve_bundle` | `None` | Bundle shape for a self-reserved group |
| `pg_strategy` | `"STRICT_PACK"` | Strategy for a self-reserved group |
| `wait_for_pg` | `True` | Block until the reservation is ready |
| `pg_ready_timeout_s` | `None` | Timeout for that wait |

As a context manager, the node releases a placement group it reserved. `node.task_options` returns the scheduling options that place an actor, such as a [PitonToolServer](tool_server.md), on the node's bundle.

## Calling members

Each member declares `resources={"openpiton": 1}`. A node instance binds the root:

| Call | Behaviour |
|---|---|
| `node.build.chia_remote(cfg)` | Runs remotely on the node's bundle; returns a Ray reference |
| `node.build.chia_remote_blocking(cfg)` | Same, and returns the value |
| `node.build(cfg)` | Runs in the calling process |
| `node.build.options(**opts)` | Returns a handle with extra Ray options and the root still bound |
| `OpenPitonWorkspaceNode.build.chia_remote(root, cfg)` | Runs remotely, unpinned |

## Members

The signatures below are the class attributes. Instance calls omit `piton_root`.

```python
sims(piton_root: str, args: str, core: str = "ariane", cwd: str | None = None,
     timeout_seconds: int = 3600) -> tuple[int, str, str]

configure(piton_root: str, x_tiles: int = 1, y_tiles: int = 1, core: str = "ariane",
          network_config: str = "2dmesh_config",
          config_rtl: tuple[str, ...] = ("MINIMAL_MONITORING",),
          caches: dict[str, tuple[int, int]] | None = None,
          address_map: str | None = None, extra_flags: tuple[str, ...] = (),
          sys: str = "manycore", timeout_seconds: int = 300) -> PitonConfig

build(piton_root: str, config: PitonConfig, sim_type: str = "vlt", clean: bool = False,
      extra_build_args: tuple[str, ...] = (), timeout_seconds: int = 7200) -> PitonBuildArtifact

run(piton_root: str, config: PitonConfig, test: str, sim_type: str = "vlt",
    precompiled: bool = False, asm_diag_root: str | None = None,
    finish_mask: str | None = None, rtl_timeout: int | None = None,
    max_cycle: int | None = None, extra_run_args: tuple[str, ...] = (),
    timeout_seconds: int = 3600) -> PitonRunResult

regress(piton_root: str, config: PitonConfig, tests: tuple[str, ...], group: str = "",
        sim_type: str = "vlt", precompiled: bool = False,
        asm_diag_root: str | None = None, timeout_seconds: int = 3600) -> PitonRegressResult

put_file(piton_root: str, relpath: str, content: bytes | str) -> str

collect(piton_root: str, base_dir: str, patterns: tuple[str, ...],
        max_bytes_per_file: int | None = None) -> PitonCollectResult

clean(piton_root: str, config: PitonConfig) -> bool
```

| Member | Behaviour |
|---|---|
| `sims` | Runs `sims <args>` verbatim in `cwd`, default `$PITON_ROOT/build`, and returns `(returncode, stdout, stderr)`. `core` selects the environment prologue |
| `configure` | Returns a validated [PitonConfig](piton_config.md) with the checkout's revisions recorded |
| `build` | Builds the model, or returns the cached one |
| `run` | Runs one diag and judges it from the transcript |
| `regress` | Calls `run` serially for each entry of `tests`. `success` holds when at least one test ran and none failed. It is a single-worker fallback; fan `run` out across workers for parallel regressions |
| `put_file` | Writes `content` to `<piton_root>/<relpath>`, creates parent directories, and returns the path. A `relpath` that escapes the checkout raises `ValueError` |
| `collect` | Globs `patterns` under `base_dir`, which is absolute or relative to the root; `**` is recursive. Skips matches outside `base_dir`. Files over `max_bytes_per_file` go to `skipped`; `0` skips every non-empty file, and `None` sets no cap |
| `clean` | Removes the configuration's model directory and returns whether it existed. `sims -clean` removes only VCS output (`csrc`, `simv`, `simv.daidir`, `AxisWork`), never `obj_dir` |

`verilator_version_text(root, core="ariane", timeout_seconds=120) -> str` is a plain static method. It returns `verilator --version` as seen inside OpenPiton's environment and caches successful results per `(root, core)`.

### configure

A given `address_map` replaces `piton/verif/env/manycore/devices_ariane.xml`, the checkout's single copy of the simulation device map. `configure` then runs four probes concurrently, and a failed git probe records `""`:

| Probe | Field |
|---|---|
| `git rev-parse HEAD` | `source_rev` |
| `git rev-parse HEAD:piton/design/chip/tile/ariane` | `ariane_rev` |
| `git diff -- piton/verif/env/manycore` | `diff` |
| `verilator --version`, inside OpenPiton's environment | `verilator_version` |

It returns the resulting `PitonConfig`, whose construction raises `ValueError` on an invalid mesh, core, network, or cache. A `caches` value of `None` or an empty mapping selects `DEFAULT_CACHES`.

### build

1. Rejects a `sim_type` outside `vlt`, `vcs`, `ncv`, `icv`, `msm`, and `riv` with `ValueError`. Only `vlt` (Verilator) is license-free.
2. Raises `ValueError` when `git diff -- piton/verif/env/manycore` differs from `config.diff`, for example after another `configure(address_map=...)` call on the checkout. A directly constructed config has `diff=""`, so it passes only while that command prints nothing.
3. Returns the cached model, if one exists.
4. With `clean=True`, deletes the model's `obj_dir` and marker.
5. For `sim_type="vlt"`, adds `--no-timing` when Verilator is version 5 or later, unless an `extra_build_args` entry already contains `timing`. The version comes from `config.verilator_version`, or from `verilator_version_text()`.
6. Runs, in `$PITON_ROOT/build`:

```text
sims <config.sims_flags()> -build_id=<config.build_id> -<sim_type>_build [-<sim_type>_build_args=<arg> ...]
```

`success` requires exit status 0 and the model binary. A failed build carries a [build_failure_reason](parsers.md) tag, or `no_model_binary` when `sims` exits 0 without a binary.

### Build cache

A successful build writes the marker file `.mace_build_ok`, containing `config.key`, into the model directory. With `clean=False`, `build` returns at once, without running `sims`, when the marker and the binary both exist. That artifact has `reused=True` and `wall_time_s=0.0`. The marker is the signal, not the binary, because a worker killed mid-link can leave a truncated binary.

`config.build_id` covers configuration, not source edits (see [PitonConfig](piton_config.md)). After an RTL or testbench change, rebuild with `clean=True` or remove the model with `clean()`.

### run

For the default `sys="manycore"`, `run` executes this command in a fresh run directory:

```text
sims <config.sims_flags()> -build_id=<config.build_id> [-precompiled] [-asm_diag_root=<dir>]
     -finish_mask=<mask> [-rtl_timeout=<n>] [-max_cycle=<n>] <extra_run_args> -<sim_type>_run <test>
```

`finish_mask` defaults to `config.finish_mask`, one `1` per tile, so a multi-tile run passes only when every hart hits the good trap. `precompiled=True` makes `sims` look in `$ARIANE_ROOT/tmp/riscv-tests/build` for a prebuilt riscv-tests ELF instead of compiling the source. `asm_diag_root` adds a directory to search for the diag source. For another `sys`, `run` drops the manycore options and the test name; pass test selection in `extra_run_args`.

The verdict comes from `parse.sim_verdict()` over the full `sim.log`, or over stdout when there is no `sim.log`. A call that timed out without a verdict gets `"timeout"`. `success` is `returncode != -1 and verdict == "pass"`, because an RTL simulation exits 0 whether or not the program passed.

## Directory layout

```text
$PITON_ROOT/build/                     working directory for sims and build()
  manycore/                            config.sys
    mace_<12 hex digits>/              config.build_id (model_dir)
      .mace_build_ok                   success marker
      obj_dir/Vcmp_top                 Verilator model (binary_path)
      runs/
        <test>-<epoch ms>-<8 hex>/     run_dir, one per run() call
          sim.log  status.log  fake_uart.log  mem.image  symbol.tbl  diag.exe
```

The run directory name replaces `/` in the test name with `_`. For another `sys`, the model lives under `build/<sys>/`, and the binary is the single executable `V*` file in `obj_dir`.

## Timeouts and processes

| Member | Default `timeout_seconds` |
|---|---|
| `sims` | 3600 |
| `configure` | 300, per probe |
| `build` | 7200 |
| `run` | 3600 |
| `regress` | 3600, per test |

Each command runs under `bash -lc` in its own session. On timeout, the adapter kills the whole process group, keeps the partial output, returns `returncode=-1`, and appends `sims timed out after <N>s` to stderr. A launch failure also returns `-1`, without that marker. On `KeyboardInterrupt`, the adapter kills the process group and re-raises.

## Environment

Each command starts by exporting `PITON_ROOT` and sourcing `piton/piton_settings.bash`. The `sparc` and `pico` cores need nothing more. For `core="ariane"`, the prologue also sets:

| Variable | Value |
|---|---|
| `ARIANE_ROOT` | `$PITON_ROOT/piton/design/chip/tile/ariane/`, with the trailing slash |
| `RISCV` | The existing value, else `$HOME/scratch/riscv_install` |
| `VERILATOR_ROOT` | `$ARIANE_ROOT/tmp/verilator-4.014/`, only when unset and that Verilator is executable |
| `LIBRARY_PATH` | `$RISCV/lib` |
| `LD_LIBRARY_PATH` | `$RISCV/lib`, prepended to the existing value |

After sourcing, the Ariane prologue prepends `$RISCV/bin`, and `$VERILATOR_ROOT/bin` when set, to `PATH`.

## Result types

### PitonBuildArtifact

| Field | Type | Meaning |
|---|---|---|
| `success` | `bool` | Exit status 0 and model binary present |
| `returncode` | `int` | `sims` exit status; `-1` on timeout |
| `config` | `PitonConfig` | The configuration built |
| `sim_type` | `str` | Simulator selector |
| `model_dir` | `str` | `$PITON_ROOT/build/<sys>/<build_id>` |
| `binary_path` | `str` | Model binary; `""` when the build failed |
| `wall_time_s` | `float` | Build wall time; `0.0` when reused |
| `verilator_version` | `str` | Version text behind the `--no-timing` decision |
| `cache_key` | `str` | `config.key` |
| `failure_reason` | `str` | Failure tag; `""` on success |
| `reused` | `bool` | `True` when served from the build cache |
| `stdout`, `stderr` | `str` | Last 8000 characters of each stream |

### PitonRunResult

| Field | Type | Meaning |
|---|---|---|
| `success` | `bool` | `returncode != -1` and `verdict == "pass"` |
| `returncode` | `int` | `sims` exit status; `-1` on timeout |
| `test` | `str` | Diag name |
| `sim_type` | `str` | Simulator selector |
| `run_dir` | `str` | This run's directory |
| `verdict` | `Verdict` or `None` | `"pass"`, `"fail"`, `"timeout"`, or `"maxcycles"`; `None` when no verdict line matched |
| `sim_time` | `int` or `None` | Simulation time stamped on the verdict line |
| `cycles` | `int` or `None` | `Cyc=` from `status.log` |
| `exec_cycles` | `int` or `None` | `ExecCyc=` from `status.log` |
| `wall_time_s` | `float` | Run wall time |
| `sim_log_tail` | `str` | Last 8000 characters of `sim.log` |
| `status_log` | `str` | Last 8000 characters of `status.log` |
| `fake_uart` | `str` | Last 8000 characters of `fake_uart.log`, the program's UART output |
| `stdout`, `stderr` | `str` | Last 8000 characters of each stream |

The `status.log` from these configurations has no `Cyc=` field, so `sim_time` is the duration measure. The static method `PitonRunResult.decide(returncode, verdict)` holds the pass rule.

### Other results

| Type | Fields |
|---|---|
| `PitonRegressResult` | `success`, `group`, `sim_type`, `num_tests`, `num_failures`, `results` (a list of `PitonRunResult`), `results_dir` (`<model_dir>/runs`), and `report`, which `regress` leaves empty |
| `PitonCollectResult` | `base_dir`, `files` (relative path to text), `skipped` (relative path to size, for files over the cap), and `listing` (relative path to size, for every match) |
