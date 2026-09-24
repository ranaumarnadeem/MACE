% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Interactive Shell

Installing MACE with pip puts the `mace` command, built from `mace/cli/`, on `PATH`.
Its `shell` subcommand works like the Yosys and OpenROAD shells: commands accumulate settings, and `run` executes the MACE loop on them.

## Commands

| Command | Purpose |
|---|---|
| `mace init` | Set up backend credentials and check the environment. |
| `mace shell` | Start the interactive shell, or run a command script. |
| `mace results` | Read the run database; see [Results and Metrics](Results_and_Metrics.md). |
| `mace cluster up CONFIG_FILE` | Run `chia up CONFIG_FILE`. `--yes`/`-y` skips the prompt, and `--dry-run` prints the plan without provisioning. |
| `mace cluster down CONFIG_FILE` | Run `chia down CONFIG_FILE`. `--yes`/`-y` skips the prompt. |
| `mace cluster status` | Run `ray status`. |

The `cluster` commands return the child's exit code, or 127 when `chia` or `ray` is not on `PATH`.
After `cluster down` on GCP, confirm the teardown with `gcloud compute instances list`.

## Credentials with mace init

```bash
mace init --backend opencode --api-key <key> --env-file .env.mace
```

| Option | Default | Purpose |
|---|---|---|
| `--backend` | `vertex` | `vertex`, `opencode`, `claude`, or `antigravity`. |
| `--api-key` | prompted | Key for the backend. Ignored for `vertex`. |
| `--env-file` | `~/.mace/.env` | Env file to write. |

For a key-based backend, `init` merges one line into the env file, sets owner-only permissions, and prints the variable it wrote: `OPENCODE_API_KEY`, `ANTHROPIC_API_KEY`, or `ANTIGRAVITY_API_KEY`.
The repository's `.gitignore` excludes `.env.mace`, so `--env-file .env.mace` keeps a project-local credentials file out of git.
For `vertex`, `init` writes nothing and checks for the credentials that `gcloud auth application-default login` saves, `~/.config/gcloud/application_default_credentials.json` on Linux.

`init` then checks that `verilator`, `riscv64-unknown-elf-gcc`, and `git` are on `PATH` and that `ray` and `chia_openpiton` import, and prints the `mace shell` command to run next.

## Starting the shell

```bash
mace shell --piton-root ~/openpiton --backend vertex
mace shell --piton-root ~/openpiton --backend opencode --api .env.mace
```

| Option | Default | Purpose |
|---|---|---|
| `--piton-root` | required | OpenPiton checkout. |
| `--api` | none | Env file to load. Required for every backend except `vertex`. |
| `--backend` | `vertex` | LLM backend. |
| `--model` | `gemini-2.5-flash` on `vertex` | Model name; written to `MACE_LLM_MODEL`. |
| `--db-path` | `runs/mace_cli.db` | Run database. |
| `--script`, `-c` | none | Run commands from a file instead of the prompt. |

The shell loads every `KEY=VALUE` line of the `--api` file into the environment, and exits before starting Ray if the file is missing or lacks the backend's variable.
For `vertex`, it sets `GOOGLE_CLOUD_PROJECT` to `mace-508004` when the variable is unset, so export your own project first.
The shell starts its own local Ray instance.

## Shell commands

| Command | Effect |
|---|---|
| `read_verilog <file> [file2 ...]` | Checks that the files exist. It does not add them to the build. |
| `top_module <name>` | Declares the top module and checks it against the supported cores. |
| `read_spec <file>` | Reads the objective, and optionally the workloads and core, from a text file. |
| `set_core <N>` | Targets N tiles and picks the mesh. |
| `run [-coverage]` | Runs the loop. `-coverage` adds line coverage to this and every later `run` in the session. `-verbose` is accepted and has no effect. |
| `write_report > <name>.rpt` | Writes the last run's report to a file. |
| `help [command]`, `h` | Lists the commands, or shows the help of one. |
| `exit`, `quit`, Ctrl-D | Leaves the shell. |

After each iteration, `run` prints each task's build status and its model or run directory, plus the simulation log tail for `workload` tasks.
When the loop ends, it prints the run ID, the status, and any post-mortem, then the per-module table and the coverage report when they apply.
Ctrl-C during `run` returns to the prompt with the session intact.
The shell keeps command history in `~/.mace/.shell_history`.

## Core compatibility checks

`top_module` matches the name, ignoring case, against these substrings:

| Substring | Core |
|---|---|
| `ariane`, `cva6` | `ariane` |
| `sparc`, `opensparc` | `sparc` |
| `picorv32`, `pico` | `pico` |

When nothing matches, `run` builds nothing and prints a static post-mortem with the assessment `likely_hardware_limitation`.
OpenPiton has no generic core-to-NoC bridge, so each core needs a hand-written L15 adapter.
[Adding a Core](../05_mace_cores/adding_a_core.md) describes that work.

## Session defaults

For each setting you did not give, `run` uses the workload `barrier_atomic.c`, the objective `Verify the gate workload passes.`, the core `ariane`, and a 1x1 mesh.
It always uses the default `Budget` from [Running the Loop](Running_the_Loop.md), and it builds against the single `--piton-root` checkout.

`set_core N` picks the factor pair of N closest to square, such as 2x2 for 4 tiles and 4x2 for 8 tiles.
Each axis holds at most 256 tiles.
It prints the note stored in `KNOWN_MESH_OUTCOMES` (`mace/cli/session.py`) for 1, 4, and 16 tiles, and marks other counts as unvalidated.

`read_spec` reads `objective:`, `workloads:` (comma-separated), and `core:` lines.
A file with none of these keys becomes the objective verbatim.

```text
objective: Verify the barrier_atomic.c gate workload passes on a 2x2 Ariane mesh.
workloads: barrier_atomic.c
core: ariane
```

## Script mode

`--script` runs one command per line and skips blank lines and full-line `#` comments.
The shell reports a failing line and moves on to the next.

```text
# session.mace
read_spec spec.txt
set_core 4
run -coverage
write_report > result.rpt
```

```bash
mace shell --piton-root ~/openpiton --backend vertex -c session.mace
```
