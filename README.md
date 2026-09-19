# MACE

Multi-core Agentic Co-design Engine — extends [CHIA](https://github.com/ucb-bar/chia)
with support for [OpenPiton](https://github.com/PrincetonUniversity/openpiton), then
builds an agentic workflow on top for autonomous multicore construction and
verification. See [`CHIA_proposal.pdf`](CHIA_proposal.pdf) for the full proposal
and [`paper/mace_paper.pdf`](paper/mace_paper.pdf) for the full write-up and results.

MACE is two phases: **Phase 1** (`chia_openpiton/`) teaches CHIA to configure,
build, simulate, run, and collect results from OpenPiton hardware designs.
**Phase 2** (`mace/`) is an agentic loop built on top of that adapter: given a
hardware objective in English, it plans a task DAG with an LLM, dispatches
tasks in parallel across real checkouts, verifies every change against real
Verilator simulation, diagnoses failures, and replans — until the design
passes or its budget runs out.

New to the project? [`docs/TECHNICAL_GUIDE.md`](docs/TECHNICAL_GUIDE.md) is a
much longer walkthrough written for exactly that — architecture, the CHIA/Ray/
OpenPiton concepts underneath it, every real bug found along the way and why,
current status, and concrete things left to do.

## Layout

- `chia_openpiton/` — the CHIA platform adapter. Imports **nothing** from `mace`,
  so it can be dropped into an upstream CHIA checkout as `chia/openpiton/`
  unchanged. Modelled on `chia.esp.esp_workspace.EspWorkspaceNode`.
- `mace/` — Phase 2: the agentic loop, its agents, gate workloads and metrics.
  Includes a `unit_test` task kind: scaffold, adapt, and build a standalone
  Verilator unit test for one RTL module via OpenPiton's own `create_env.py`,
  with a real, single-file-scoped MCP tool (`mace/tools.py`) letting the
  agent reconcile the scaffolded testbench against the module's real ports.
- `examples/` — runnable demos, including the full end-to-end loop and the
  paper's baselines.
- `scripts/` — the OpenPiton toolchain patch script and standalone diagnostics.
- `cluster/` — CHIA cluster configs (`local.yaml`: WSL head + optional GCP worker).
- `paper/` — the 4-page paper (`mace_paper.tex` / `.pdf`).
- `docs/` — the technical guide, project handoff notes, and the CHIA issue draft.
- `dockerfiles/` — a worker image (built once, since dropped in favor of bare-VM
  `setup_commands` — see the technical guide for why).

This repo does **not** fork CHIA. CHIA is a plain dependency installed from its
own clone, the same pattern CHIA's docs describe for companion node libraries.

## Install

```bash
conda create -n chia_env -c conda-forge --override-channels python=3.10.19
conda activate chia_env

git clone https://github.com/ucb-bar/chia.git
pip install -e ./chia          # not on PyPI at the revision we build against

pip install -e ".[test]"       # this repo
pytest chia_openpiton/test -q  # tier 0: no Ray, no OpenPiton needed
```

To actually build and simulate RTL (anything beyond tier-0 tests), you also
need a real OpenPiton checkout and a patch pass over it — see
[`chia_openpiton/README.md`](chia_openpiton/README.md) for the toolchain
requirements, then:

```bash
git clone https://github.com/PrincetonUniversity/openpiton.git
git -C openpiton submodule update --init --recursive piton/design/chip/tile/ariane
bash scripts/patch_openpiton.sh /path/to/openpiton   # idempotent; fixes 8 real
                                                       # toolchain/checkout/RTL bugs
```

**Or skip the toolchain setup above entirely with Nix.** `flake.nix`
reproducibly pins the whole non-Python side of this — the RISC-V
toolchain, every system package `chia_openpiton`'s own patches/build
assume, and Verilator (from its own separate pin: a real tagged release,
not a "devel" snapshot, *and* recent enough to clear a real internal
Verilator crash this project hit on its own RTL — a plain "tagged
release" isn't sufficient on its own; see `flake.nix`'s own
`nixpkgs-verilator` input comment for the full account):

```bash
nix develop   # first run creates .venv and tells you the two pip installs above
```

`chia` still installs the normal way inside that shell (`pip install -e
/path/to/chia`) — it's intentionally not part of the Nix closure, since
it isn't on PyPI at the revision this project builds against (see
`pyproject.toml`'s own comment). Verified end to end: a real `nix
develop` builds cleanly and the resulting `riscv64-unknown-elf-gcc`
actually compiles and links RV64 code.

Nix only replaces the *toolchain* setup above — the OpenPiton
`git clone` + `bash scripts/patch_openpiton.sh` step is still required
either way (the shell reminds you if `PITON_ROOT` is set and unpatched).

## Using MACE

**The `mace` CLI** is the easiest way in — a real interactive shell,
modeled on Yosys/OpenROAD's own command style:

```bash
mace init --backend opencode --api-key <key>   # once: writes ~/.mace/.env + a real env check
mace shell --piton-root /path/to/openpiton --api ~/.mace/.env
```

Google Vertex/Gemini works too, and needs no `--api` file at all if
`gcloud auth application-default login` is already set up (ADC) — confirmed
with a real, passing end-to-end loop run:

```bash
mace shell --piton-root /path/to/openpiton --backend vertex --model gemini-2.5-flash

mace> read_verilog my_core.v
mace> top_module my_core_top
mace> read_spec objective.txt
mace> set_core 4
mace> run
mace> write_report > result.rpt
```

`top_module` is checked against the cores chia_openpiton actually has a
working adapter for (`ariane`/`sparc`/`pico`) — if it doesn't match one,
`run` doesn't fake an attempt, it immediately explains why, structurally
(see `docs/TECHNICAL_GUIDE.md` §12). Below are four more ways to use MACE
directly, from lowest-level to highest-level, if you want more control than
the shell gives you.

**1. Drive the adapter directly** — configure, build, and run one RTL design,
no LLM involved:

```python
from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

node = OpenPitonWorkspaceNode("/path/to/openpiton")
cfg = get(node.configure.chia_remote(x_tiles=1, y_tiles=1, core="ariane"))
art = get(node.build.chia_remote(cfg))
res = get(node.run.chia_remote(cfg, "hello_world.c"))
assert res.verdict == "pass"   # read from the simulator's own transcript
```

**2. Run the full MACE loop** — plan → dispatch → verify → triage → replan,
recorded to a metrics database:

```bash
python examples/mace_end_to_end.py \
    --piton-root /path/to/openpiton \
    --model opencode/big-pickle \
    --max-iterations 3
```

Add `--piton-root-2 <second checkout>` to exercise real parallel fan-out.
Prints per-iteration results and the five metrics the proposal promises
(successful tasks, iterations, failures recovered, execution time, compute
cost) at the end. Everything is also recorded to
`runs/mace_end_to_end.db` (`--db-path` to change it).

**3. Run the baselines** — the same objective, without the loop, for
comparison (see `paper/mace_paper.pdf` §6 for what these numbers mean):

```bash
bash scripts/local_baselines_b_and_c_test.sh /path/to/openpiton
```

Runs baseline (b) (`examples/baseline_one_shot_llm.py` — a single LLM prompt
proposes a configuration once, with no tools and no retry, applied directly
with no verification loop) then baseline (c) (`examples/mace_end_to_end.py`
— the full loop) back to back against the same checkout, same objective, for
a clean comparison; each can also be run standalone. Real result from the
most recent run: (b) built but the run itself timed out (`verdict=timeout`,
failed, 178s total); (c) passed, 4/4 tasks, one iteration, 197.7s — the
verification loop cost almost no extra wall-clock here and is the difference
between passing and not. Manual mesh scaling (baseline (a)) has no dedicated
script; `scripts/local_2x2_build_test.py` is the closest thing, a hand-run
multi-tile attempt with a full log of what happened, kept for its
historical/diagnostic value rather than as a clean reusable baseline runner.

**4. Read the results** — `paper/mace_paper.pdf` has the full write-up; the
raw data behind it is in `runs/mace_end_to_end.db` (SQLite; `mace.metrics.summary()`
is the reader) and in the run logs each script above prints.

## What's proven, honestly

Everything below is a real hardware result — an actual Verilator RTL
simulation reaching an actual pass/fail verdict, not a mock or a self-report
from an agent. See the paper for full detail and for what did *not* pass,
stated just as precisely.

- The adapter builds and runs real Ariane (RV64) and SPARC designs, and
  passes a full parallel-build acceptance test across two real checkouts.
- The full MACE loop has completed multiple real end-to-end runs against real
  hardware, recording real cost/time/task metrics.
- A third core, PicoRV32, was added to the adapter and now genuinely
  **passes** under Verilator (`Simulation -> PASS (HIT GOOD TRAP)`) — a first
  for this core under any simulator, by anyone (OpenPiton's own CI only ever
  builds it, never runs it). Getting there took three real, independently
  waveform-verified RTL/testbench bugs, all fixed: picorv32's own
  `resetn`/`booted` self-boot gate (it was waiting forever for an interrupt
  nothing in a bare config ever sends), the manycore monitor's
  `active_thread` tracking for pico's tile, and a real-silicon BIST
  self-clear race that was silently discarding pico's first, very early
  memory writes.
- Unit-level testing: MACE can scaffold, adapt, and build a standalone
  Verilator test for one RTL module — not just the whole chip — via
  OpenPiton's own `create_env.py`. Proven end to end with a real LLM (Gemini
  on Vertex): given a module it had never seen (`alarm_counter`) and a
  single-file-scoped edit tool, it correctly reconciled the scaffolded
  testbench's placeholder ports/widths/module type against the real module on
  the first try, and the resulting build passed.
- A real, previously-unknown regression in OpenPiton's own `sims` build
  script was found and fixed: every manycore build was silently linking
  against the wrong C++ driver (the new unit-test one instead of the
  original manycore driver), breaking real Ariane/SPARC builds with
  undefined DPI references. Root cause and fix are in
  `scripts/patch_openpiton.sh`.
- A real dispatch failure was found, filed upstream as
  [ucb-bar/chia#72](https://github.com/ucb-bar/chia/issues/72), and resolved: not a CHIA
  scheduler bug as first suspected, but a launch-configuration gap on our own side (a manually
  launched driver needs three proxy env vars that `chia job submit` sets automatically) —
  confirmed by a CHIA maintainer and verified directly, taking a task from a 6-minute hang to a
  2-second real execution. See `docs/TECHNICAL_GUIDE.md` §11 for the full account.

## More detail

- [`docs/TECHNICAL_GUIDE.md`](docs/TECHNICAL_GUIDE.md) — the full technical
  walkthrough and onboarding doc: architecture, background concepts, current
  status, open problems, how to actually run everything.
- [`chia_openpiton/README.md`](chia_openpiton/README.md) — the adapter's own
  docs (worker requirements, gotchas, upstreaming checklist), kept
  self-contained so this directory can move into a CHIA PR unchanged.
- [`docs/PROJECT_HANDOFF.pdf`](docs/PROJECT_HANDOFF.pdf) — an earlier
  point-in-time snapshot (Sep 8); superseded by the technical guide above but
  kept for its detailed GCP investigation timeline.
