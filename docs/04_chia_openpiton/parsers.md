% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Transcript Parsers

`chia_openpiton/parse.py` holds pure functions that take text and return data. They do no I/O, start no subprocesses, and need no Ray. These functions decide whether a simulation passed, so they are tested against captured logs in `chia_openpiton/test/fixtures/` with no toolchain present.

The patterns come from OpenPiton's source. `piton/verif/env/manycore/pc_cmp.v.pyv` prints the PASS line once every hart in the finish mask hits the good trap. `piton/verif/env/manycore/monitor.v.pyv` owns the `fail` task and the max-cycles abort. regreport renders `status.log` and the regression summary.

## Verdicts

An RTL simulation exits 0 whether or not the program passed, so the adapter reads the verdict from the transcript. `sim_verdict()` classifies these lines:

| Transcript line | Verdict |
|---|---|
| `179911750: Simulation -> PASS (HIT GOOD TRAP)` | `"pass"` |
| `1234500 : Simulation -> FAIL(HIT BAD TRAP)` | `"fail"` |
| `1234500 : Simulation -> FAIL(TIMEOUT)` | `"timeout"` |
| `1000250 : Simulation -> (terminated by reaching max cycles = 2000)` | `"maxcycles"` |
| `Info: spc(0) thread(3) -> timeout happen`, with no verdict line | `"timeout"` |
| None of these | `None` |

The PASS line has no space before the colon and the FAIL line has one; the patterns accept both. The checks run in a fixed order: FAIL, then max cycles, then PASS, then `-> timeout happen`. A FAIL whose message contains `TIMEOUT`, matched case-insensitively, counts as `"timeout"`. The order makes the parser fail closed: a FAIL line wins over a PASS line in the same transcript, and `timeout happen` never overrides a verdict line.

## Function reference

```python
# sim.log
sim_verdict(text: str) -> Verdict | None
sim_time(text: str) -> int | None
fail_reason(text: str) -> str
max_cycles(text: str) -> int | None

# status.log
status_diag(text: str) -> tuple[str, str] | None
cycles(text: str) -> int | None
exec_cycles(text: str) -> int | None
num_tiles(text: str) -> int | None

# regreport summary and verilator_coverage
regress_summary(text: str) -> dict[str, object]
coverage_summary(text: str) -> dict[str, object]

# sims output
sims_die(text: str) -> str
model_dir(text: str) -> str
build_failure_reason(stdout: str, stderr: str = "") -> str

# Verilator
verilator_version(text: str) -> tuple[int, int] | None
needs_no_timing(version_text: str) -> bool

# diaglists and transcripts
diaglist_group(text: str, group: str) -> tuple[DiagEntry, ...]
first_divergence(reference: str, actual: str) -> tuple[int, str, str] | None
```

| Function | Returns |
|---|---|
| `sim_verdict` | `"pass"`, `"fail"`, `"timeout"`, `"maxcycles"`, or `None` for an empty transcript or one without a verdict |
| `sim_time` | The simulation time that prefixes the verdict line, such as `179911750` |
| `fail_reason` | The message inside `Simulation -> FAIL(...)`, such as `HIT BAD TRAP`, or `""` |
| `max_cycles` | The count from a max-cycles abort, or `None` |
| `status_diag` | `(diag_name, status_text)` from the `Diag:` line. The status is regreport's text, such as `PASS`, `FAIL`, `Timeout`, `MaxCycles Hit`, `FAIL (Monitor)`, or `Unknown (No Status)` |
| `cycles`, `exec_cycles`, `num_tiles` | The `Cyc=`, `ExecCyc=`, and `NumTiles=` values, or `None` |
| `regress_summary` | A dict with `passed`, `counts`, and `diag_count`, parsed from a `regreport ... -summary` table. `passed` is `True` or `False` from a `REGRESSION PASSED` or `REGRESSION FAILED` line, else `None`. `counts` maps each status row present (`PASS`, `FAIL`, `Diag Problem`, `License Problem`, `MaxCycles Hit`, `Socket Problem`, `Timeout`, `LessThreads`, `Simics Problem`, `Performance`, `Killed By Job Q`, `Unknown`, `UnFinished`, `flexlm error`) to its count. `diag_count` comes from the `Diag Count:` row |
| `coverage_summary` | `{"hit": 8749, "total": 24311, "percent": 35.0}` from the `verilator_coverage --annotate` line `Total coverage (8749/24311) 35.00%`. All three values are `None` when the line is missing, so a failed `verilator_coverage` run never reads as 0% |
| `sims_die` | The message from `sims: Caught a SIGDIE. <message> at <file> line N.`, without the Perl file and line suffix, or `""` |
| `model_dir` | The path from `sims: creating model directory <path>`, or `""` |
| `build_failure_reason` | A failure tag from the table below, or `""` |
| `verilator_version` | `(major, minor)` from `verilator --version`, for release strings (`Verilator 4.038 2020-07-11 rev ...`) and development builds (`Verilator 5.049 devel rev ...`), or `None` |
| `needs_no_timing` | `True` for Verilator 5 and later, which refuse OpenPiton's bare `#1` delays without `--timing` or `--no-timing`. `False` for Verilator 4, which has no such flag, and for text it cannot parse |
| `diaglist_group` | The tests of one diaglist group, as `DiagEntry` values |
| `first_divergence` | The 1-based line number and both lines at the first line where the texts differ. `None` when one text is a line-for-line prefix of the other, which includes identical texts |

[run()](workspace_node.md) calls `sim_verdict` and `sim_time` on the full `sim.log`, and `cycles` and `exec_cycles` on `status.log`. The `status.log` from these configurations has no `Cyc=` field, so `sim_time` is the duration measure. `build()` calls `needs_no_timing` and `build_failure_reason`. [PitonToolServer](tool_server.md) calls `first_divergence`, and the MACE shell calls `coverage_summary` (see [Code Coverage](../01_mace_user/Code_Coverage.md)).

## Build failure tags

`build_failure_reason` searches stdout and stderr together and returns the first tag that matches:

| Order | Tag | Matches |
|---|---|---|
| 1 | `verilator_needs_timing_flag` | `%Error-NEEDTIMINGOPT` |
| 2 | `verilator_bad_option` | `%Error: Invalid option: <option>` |
| 3 | `pch_link_failure` | `undefined reference to` followed by a quote character and `main` |
| 4 | `verilog_error` | `%Error:` or `%Error-<CODE>:` |
| 5 | `compile_error` | A line that starts with `<file>:<line>:<col>: error:` |
| 6 | `make_failed` | A line that starts with `make: ***` or `make[<n>]: ***` |
| 7 | `sims_die:<message>` | A `sims` SIGDIE message, when nothing above matched |

The order puts specific diagnostics ahead of the generic lines that follow them. `build()` stores the tag in `PitonBuildArtifact.failure_reason`.

## Diaglists

`diaglist_group` returns the tests in one `<group>...</group>` block of a `master_diaglist_*` file, in file order, as `DiagEntry(alias: str, source: str, args: tuple[str, ...] = ())`. Tags match by name, and attributes on the opening tag are ignored. `//` starts a comment that runs to the end of the line. Nested groups are flattened into the selected group. Flags on a `<runargs ...>` line inside the group apply to each test line up to `</runargs>`, and come first in that test's `args`. On a test line, the source is the first token after the alias that does not start with `-` and contains a `.`; the other tokens follow as args, in file order. A missing group or closing tag raises `ValueError`.

For group `ariane_tile1_simple` of OpenPiton's `master_diaglist_princeton`, the first entry is:

```python
DiagEntry(alias="ariane-hello-world", source="hello_world.c",
          args=("-x_tiles=1", "-y_tiles=1", "-ariane", "-rtl_timeout", "1000000"))
```
