% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Code Coverage

MACE can build Verilator models with line coverage and report how much of the RTL a passing run exercised.
Coverage is a second measure next to the pass or fail verdict.

## Requirement

OpenPiton's C++ testbench driver, `piton/tools/verilator/my_top.cpp`, does not write coverage data by itself.
Fix 5 of `scripts/patch_openpiton.sh` adds a `VerilatedCov::write("coverage.dat")` call at exit, guarded by `#if VM_COVERAGE`, so it takes effect only in coverage builds.
Patch the checkout before you build with coverage; see [Installation](Installation.md).

## Enabling coverage

`chia_openpiton/state_def.py` defines the flag:

```python
COVERAGE_LINE_FLAG = "-vlt_build_args=--coverage-line"
```

`sims` passes `--coverage-line` to Verilator, which then instruments line coverage only.
Enable it in one of three places:

| Where | How |
|---|---|
| Interactive shell | `run -coverage`. It applies to this and every later `run` in the session. |
| Loop from Python | `MaceSpec(..., coverage=True)`. Each `config` and `workload` task's build gets `COVERAGE_LINE_FLAG` in its `extra_flags`; `unit_test` builds do not. |
| Adapter | `node.configure.chia_remote(..., extra_flags=(COVERAGE_LINE_FLAG,))`. |

The flag is part of the build ID, so a coverage model gets its own directory beside the plain model of the same mesh.
`examples/mace_end_to_end.py` has no coverage option.

## Where coverage.dat lands

The simulator writes `coverage.dat` in the run directory that the adapter creates for each simulation:

```text
$PITON_ROOT/build/manycore/<build_id>/runs/<test>-<ms timestamp>-<8 hex digits>/coverage.dat
```

`PitonRunResult.run_dir` holds this directory, and the shell prints it for each workload task.

## Reports

Annotate the data with `verilator_coverage`:

```bash
verilator_coverage --annotate <run_dir>/coverage_annotated <run_dir>/coverage.dat
```

It prints a total line and writes an annotated copy of each source file, with uncovered lines marked `%00`:

```text
Total coverage (<hit>/<total>) <percent>%
See lines with '%00' in <run_dir>/coverage_annotated
```

Use the `verilator_coverage` of the Verilator that built the model, since the `coverage.dat` format is tied to the version that wrote it.
MACE uses `--annotate`, because older releases such as 5.020 have no `--report`.

`chia_openpiton.parse.coverage_summary(text)` parses the total line into `{"hit": int, "total": int, "percent": float}`.
All three values are `None` when the text has no total line, for example when `verilator_coverage` failed.

## Coverage in the shell

After a `run -coverage` that passes, the shell finds the newest `coverage.dat` of the run, annotates it into `coverage_annotated/` beside the file, and prints the total.
`write_report` includes the total in the report.
The shell runs the first `verilator_coverage` on `PATH` if it answers `--version`, and `/usr/bin/verilator_coverage` otherwise.
