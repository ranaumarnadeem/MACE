# MACE Technical Guide

This is the deep-dive doc — written for a teammate picking up this project,
not just running it. It explains what's here, why it's built the way it is,
what's actually proven versus hoped, and what's genuinely left to do. Where
something is a CHIA or OpenPiton concept you might not already know, it's
explained here rather than assumed — that's deliberate, so you can pick this
up without a separate crash course.

For a quick "how do I run this" without the depth, see [`README.md`](../README.md)
instead. This doc is the one to actually read start to end.

Everything below reflects the repository as it stands right now — every
number, every file, every bug described was checked against the real source
or a real run while writing this, not carried forward from an earlier plan.
Where something is a genuine open question rather than a settled fact, it
says so explicitly rather than guessing.

## 1. The big picture

MACE (Multi-core Agentic Co-design Engine) is a submission to the A3 CHIA
Hackathon (Google + NVIDIA, MICRO 2026 workshop). Judging criteria: a 4-page
paper, an open-sourced CHIA loop, and its results. Deadline **Sep 20 AoE**
(~17:00 PKT Sep 21), code freeze Sep 19.

The idea: take [CHIA](https://github.com/ucb-bar/chia), an existing framework
for orchestrating agentic hardware-design workflows on Ray clusters, and
extend it to a real, large, silicon-proven manycore platform
([OpenPiton](https://github.com/PrincetonUniversity/openpiton)), then build an
autonomous agentic loop on top that can take a plain-English hardware
objective and actually get it built and verified against real RTL simulation
— including recovering from its own failures.

This is two phases, and the split matters architecturally, not just
organizationally:

- **Phase 1 — `chia_openpiton/`**: teach CHIA to configure, build, simulate,
  run, and collect results from OpenPiton. This is infrastructure — no
  agentic behavior lives here at all. It's shaped deliberately like CHIA's
  existing platform adapters (e.g. `chia.esp`) so it's realistically
  upstreamable as a PR to CHIA itself.
- **Phase 2 — `mace/`**: the actual agentic loop, built entirely on top of
  Phase 1's adapter. This is the project's real novel contribution: plan →
  decompose → dispatch in parallel → integrate → verify against real
  hardware → diagnose failures → replan → iterate.

**Hard rule, enforced by an actual test:** `chia_openpiton/` imports nothing
from `mace/`. The dependency only goes one way. `examples/` is the only place
allowed to depend on both — a driver script wiring them together, not either
package reaching into the other's concerns.

## 2. Background you'll want

### CHIA and Ray, briefly

CHIA is built on [Ray](https://www.ray.io/), a distributed-execution
framework. The pattern you'll see everywhere in this codebase:

```python
ref = some_function.chia_remote(args)   # dispatches, returns immediately
result = get(ref)                        # blocks until it's done, unwraps the result
```

`chia_remote` is CHIA's wrapper around Ray's own remote-dispatch — it runs the
function on some worker in the cluster (could be this machine, could be a
different one) and gives you back a reference you resolve later with `get()`.
The reason to dispatch-then-get as *separate statements* rather than
`get(fn.chia_remote(...))` inline isn't just style: CHIA's `chia viz` tool
statically reads your source to draw the task graph, and it only recognizes
the two-statement form. Inline it and `chia viz` renders nothing.

A **`ColocatedNode`** (what `OpenPitonWorkspaceNode` is) is CHIA's answer to
"this operation needs to keep running on the *same* worker as the last one."
OpenPiton's build state lives on disk (`$PITON_ROOT/build/manycore/<id>/`) —
if `configure()` ran on worker A but `build()` landed on worker B, B wouldn't
see A's config. A `ColocatedNode` reserves a Ray **placement group** (a
resource-shaped claim on one worker) when constructed, and every subsequent
call through that node instance is pinned to land on that same worker.

**Custom resources** (`openpiton`, `opencode_creds`, `llm`, etc.) are how you
tell Ray's scheduler what a task needs and what a worker offers. They're not
predefined — a cluster config or `ray.init(resources={...})` call declares
them, and a task requesting more of a resource than any worker advertises
just waits forever (not an error — a real gotcha the whole test suite works
around with timeouts). `{"openpiton": 1}` here specifically means "reserve
one whole checkout," not "one CPU core" or anything generic — see §4 below
for why a checkout is the actual unit of concurrency.

**Cache/bypass** is CHIA's replay mechanism: tag a `.chia_remote(...)` call
with `_chia_tag="some-key"`, and a later call with the same tag can be served
from a cache instead of re-executed. Important limit, confirmed by reading
CHIA's own source rather than assumed: this caches a call's **return value**
only. It cannot capture side effects — an LLM's file edits via a Bash tool
are invisible to it. `mace/replay.py` is built around this limit honestly:
it can replay *decisions* (what an LLM said, whether a build passed) at zero
extra cost, but git remains the actual record of what changed on disk.

### OpenPiton, briefly

OpenPiton is Princeton's open-source, silicon-proven manycore research
platform: a grid of tiles connected by a 2D-mesh network-on-chip (NoC). Each
tile is a full core-plus-cache slice; tiles are wired together to form
`x_tiles × y_tiles` mesh (a "1×1" build is a single tile, a "4×4" build is 16
tiles).

The thing that will trip you up if you assume this is AXI or TileLink: it
isn't. Every tile talks to the NoC through a proprietary, MESI-coherence-aware
protocol called **L15** (its lineage traces back to OpenSPARC T1's PCX/CPX
bus). There is no generic "core-to-NoC bridge" — each core has its own
bespoke L15 adapter, hand-written for that specific core:

- **`sparc`** (OpenSPARC T1) — the original, reference integration.
- **`ariane`** (Ariane/CVA6, RV64GC) — the core this project mainly targets.
  Its own AXI4 output is never actually used for the NoC connection; a
  separate hand-written encoder (`wt_l15_adapter.sv`) taps CVA6's *internal*
  pre-AXI cache-miss request stream instead. This core has a real coherent
  cache of its own, which is what makes its L15 integration substantial.
- **`pico`** (PicoRV32, RV32I) — added to this project's adapter as an
  extension (§7). Has no cache of its own (non-coherent), so its L15 adapter
  (`pico_l15_transducer.v`) is much simpler — but it existed in OpenPiton
  upstream, complete and unused, before this project touched it.

**Verilator** is the RTL simulator used throughout — it compiles the Verilog
design into a C++ model and runs a compiled binary rather than interpreting
gates at runtime, which is why "build" (compile the RTL) and "run" (execute a
program against the built model) are two distinct, separately-timed steps
everywhere in this codebase.

**A critical, non-obvious fact this whole adapter is built around: an RTL
simulation's process exit code tells you nothing.** It exits 0 whether the
simulated program passed, failed, or hung. The real verdict comes from the
testbench's own printed transcript — a line like `Simulation -> PASS (HIT
GOOD TRAP)` in `sim.log`, or `Simulation -> FAIL(...)`, or a "reached max
cycles" message for a hang. `chia_openpiton/parse.py`'s job is reading that
transcript; `PitonRunResult.success` is derived from the parsed verdict,
never from a return code.

**`sims`** is OpenPiton's own Perl-based build/run driver. Everything the
adapter does is really "construct the right `sims` command line and run it" —
`chia_openpiton/state_def.py`'s `PitonConfig.sims_flags()` is where a config
becomes an actual argv.

## 3. Repository map

```
MACE/
  chia_openpiton/    Phase 1: the CHIA/OpenPiton adapter. No mace/ imports.
  mace/              Phase 2: the agentic loop.
  examples/          Driver scripts — the only place depending on both halves.
  scripts/           Standalone diagnostics + the checkout patch script.
  cluster/           CHIA cluster YAML (WSL head, optional GCP worker).
  paper/             The 4-page paper (mace_paper.tex / .pdf).
  docs/              This guide, the earlier handoff PDF, the filed CHIA issue draft.
  dockerfiles/       A worker image — built once, not currently used (§8).
  runs/              Metrics DBs and run logs. Gitignored.
```

### `chia_openpiton/` module by module

| Module | Role |
|---|---|
| `state_def.py` | Frozen dataclasses. `PitonConfig` (core, mesh, network topology, cache geometry, extra flags) carries a `.key` — a sha256 over everything that changes the produced model (source revisions, Verilator version, the config itself, any uncommitted diff) — and a derived `.build_id`. Also `PitonBuildArtifact`, `PitonRunResult`, `PitonRegressResult`, `PitonCollectResult`. |
| `parse.py` | Pure text parsers — zero I/O, zero subprocess, testable with no toolchain present at all. `sim_verdict()` reads the PASS/FAIL/maxcycles markers from `sim.log`; `status_diag/cycles/exec_cycles()` read `status.log`; `build_failure_reason()` classifies a build failure; `needs_no_timing()` decides a Verilator-version-dependent flag. |
| `openpiton_workspace.py` | `OpenPitonWorkspaceNode(ColocatedNode)` — the adapter's actual surface: `configure`/`build`/`run`/`regress`/`collect`/`clean`. `build()` skips re-invoking `sims` entirely when an identical config already built successfully (a disk marker, not CHIA's cache machinery) — measured 104s → 0.00s on a repeat build. |
| `tools.py` | `PitonToolServer` — the agent-facing MCP tool surface (grep/status/collect over logs, an async build-start/poll pair since builds run minutes long). |
| `test/` | Tier-0 unit tests against captured real log fixtures, plus `test/cluster/` for tier-1 (stub hardware, real Ray) and tier-2 (real hardware) tests. |

### `mace/` module by module

| Module | Role |
|---|---|
| `spec.py` | Frozen dataclasses: `MaceSpec` (core, target mesh, gate workloads, objective, budget), `Budget` (three *independent* caps — max iterations, max USD, max wall-clock — all enforced), `Task`, `Triage`, `LoopResult`. |
| `agents.py` | Footer-line parsers. The Planner and triage agent talk back through plain `TASK:` / `DIAGNOSIS:` / `FIX:` lines embedded in ordinary prose — no CHIA LLM backend guarantees a JSON mode, so this is deliberately permissive (regex over the whole response, malformed lines dropped, not raised). |
| `loop.py` | `run_mace_step()` — the smallest real slice: one task, no fan-out, no Planner. Proves the wiring (LLM → build → run → gate) independent of any planning intelligence. |
| `integrator.py` | `integrate()` (one shared checkout, serial) and `integrate_parallel()` (one checkout per worker, dispatches a whole dependency-level's tasks before resolving any of them). This is also where replay tagging happens — see `_tag()` in `_run_batch`. |
| `planner.py` | `plan(spec, llm, feedback)` — one LLM call → parsed into a validated task DAG. `feedback` from a previous iteration's triage is appended as extra context on a replan. |
| `triage.py` | `triage(step_result, llm)` — one LLM call given a failing build/run's own log tail, asking for a `DIAGNOSIS:`/`FIX:` footer. Read-only tools only (grep/collect) — triage diagnoses, it never edits; an actual fix becomes a future Planner task. |
| `orchestrator.py` | `run_mace_loop()` — the top-level entry point. See the call chain in §5. |
| `metrics.py` | `SQLiteNode`-backed record of runs/iterations/tasks/failures. `summary()` derives the five metrics the proposal promises. |
| `llm.py` | `make_llm()` picks a backend from the `MACE_LLM` env var (`opencode` default, or `claude`/`antigravity`/`vertex`). `extract_cost_usd()` reads a per-call dollar cost where the backend's response object carries it after a remote round-trip — always `0.0` for Claude, a documented real API-shape limitation (its cost lives on the LLM instance's own local state, invisible once dispatched remotely), not a bug silently swallowed. |
| `replay.py` | Wraps CHIA's cache/bypass mechanism (see §2). Scoped honestly around its real limit — decisions only, never side effects. |
| `workloads.py` + `workloads/` | Three real gate C programs (`barrier_atomic.c`, `producer_consumer.c`, `scatter_gather.c`) plus a `CHECKSUMS` file. `verify_checksums()` runs at the start of every loop run — a task that tampered with the gate itself (by accident or otherwise) must not get to grade its own work. |

**A real, non-obvious hardware bug shaped `workloads.py`.** On this RTL/
toolchain combination, a plain `volatile` read of a shared *array element*
right after a write to it is **not reliably visible** — even from the same
hart, a few instructions later — while the identical value read back through
an atomic fetch-op always is. This was found by narrowing a "computes the
right value, then hangs forever" symptom down to a 10-line repro, one variable
at a time. Fix: every gate workload routes every shared read through an
`atomic_read()` helper, never a plain load — documented in the module's own
docstring so nobody rediscovers this by hand. If you write a new gate
workload, use `atomic_read()` for anything another hart might have written.

## 4. The one architectural constraint everything else follows from

OpenPiton's own template preprocessor (pyHP) writes generated `.tmp.v` files
**back into the source tree itself** on every build. Two builds against the
same checkout — even with different configs — corrupt each other. This one
fact is why:

- A worker resource is `{"openpiton": 1}` **per checkout**, not per machine.
  A worker advertising 2 slots must actually host two separate checkouts.
- Phase 2's parallel task dispatch gives each task its own checkout, not a
  shared one with locking.
- `chia_openpiton`'s own parallel-build acceptance test needs two real,
  separate checkouts to prove real concurrency (176s parallel vs. 324s
  serial-equivalent — proven, real hardware).

If you're ever tempted to point two workers at the same OpenPiton directory
to save disk space: don't. This is the one rule that, if broken, produces
confusing, intermittent, hard-to-attribute failures rather than a clean error.

## 5. The call chains, traced through real code

**One adapter operation** (no LLM, no loop — just the adapter):

```python
node = OpenPitonWorkspaceNode("/path/to/openpiton")   # binds root, reserves a placement group
cfg = get(node.configure.chia_remote(x_tiles=1, y_tiles=1, core="ariane"))
art = get(node.build.chia_remote(cfg))                 # sims ... -vlt_build -build_id=<cfg.build_id>
res = get(node.run.chia_remote(cfg, "hello_world.c"))
assert res.verdict == "pass"                           # read from sim.log, NOT from a return code
```

**One full loop run**, the actual sequence `mace.orchestrator.run_mace_loop`
executes — every line below is a real call in the current source, not a
simplified paraphrase:

```python
run_id = mace.metrics.start_run(db, spec)
mace.workloads.verify_checksums()                # stop immediately if the gate itself was edited

for iteration in range(spec.budget.max_iterations):
    # budget check (wall_s, then usd) happens here, BEFORE the iteration starts
    tasks = mace.planner.plan(spec, llm, feedback)          # 1 LLM call -> TASK: lines -> validated DAG
    results = mace.integrator.integrate_parallel(            # tagged with run_id/iteration for replay
        piton_roots, spec, tasks, llm, run_id=run_id, iteration=iteration)
    # per dependency level, per batch of len(piton_roots) tasks:
    #   llm.prompt.chia_remote(...) -> build.chia_remote(...) -> run.chia_remote(...)
    #   (every call in a batch is DISPATCHED before any of them is RESOLVED)
    mace.metrics.record_iteration(db, run_id, iteration, results, wall_s, usd)

    if all(r.passed for r in results):
        status = "passed"; break

    failed = first non-passing result
    diagnosis = mace.triage.triage(failed, llm)               # 1 LLM call -> DIAGNOSIS:/FIX:
    mace.metrics.record_failure(db, run_id, iteration, failed.task.id, diagnosis)
    feedback = f"Task {failed.task.id} failed: {diagnosis}"    # -> next iteration's plan() call

if status == "passed" and any earlier iteration recorded a failure:
    mace.metrics.mark_all_recovered(db, run_id)
mace.metrics.finish_run(db, run_id, status)
```

## 6. What's proven vs. what isn't — read result claims through this lens

The project draws a real, deliberate distinction between four test tiers.
When you read "X is done" anywhere in this repo, check which tier backs it —
"proven" is used carefully throughout, and you should use it the same way.

| Tier | Needs | Proves |
|---|---|---|
| **0 — local, no Ray** | Nothing real; a stub `sims` script | Parsers against real log fixtures, config validation, exact CLI argv shape, error paths |
| **1 — live Ray, stub tool** | A local Ray cluster, stub `sims` staged on a worker | Real placement-group pinning, real remote dispatch/fan-out, real cache/bypass round-trips — but a **fake** hardware verdict |
| **2 — real tool** | A real OpenPiton checkout, Verilator, the toolchain | Genuine RTL builds and genuine simulated pass/fail verdicts |
| **loop (FakeLLM)** | Nothing real — a scripted response queue | Loop control flow (plan → dispatch → gate → triage → replan) independent of any real model's behavior |

Tier 0/1 prove the *wiring* is correct. Tier 2 proves it *actually works*.
Both matter, but don't let a green tier-1 test stand in for a tier-2 claim.

## 7. Current status, section by section

### Phase 1 — the adapter: complete

`state_def.py`, `parse.py`, `openpiton_workspace.py`, `tools.py` are all real
and tested. Acceptance status:

| # | Check | Status |
|---|---|---|
| 1 | Local Ariane: `configure(1,1)` → `build` → `run(hello_world.c)` → pass | **real hardware, proven** |
| 2 | 2×2 Ariane build on a real GCP worker | **blocked** — real CHIA bug, filed upstream (§9) |
| 3 | Fan-out: parallel builds across two checkouts | **real hardware, proven** — 176s vs 324s serial |
| 4 | `chia viz` renders the example's task graph | proven |
| 5 | Tier-0 suite green on fixtures | proven, currently green |

Four real, non-obvious bugs were found only by running the actual toolchain,
and are now permanently fixed in `scripts/patch_openpiton.sh` (idempotent —
safe to re-run on any checkout):

1. **binutils 2.38+** split `zicsr`/`zifencei` out of base RV64I — the 2019-era
   boot ROM assembly no longer assembles without spelling them out explicitly.
2. **GCC 15+ defaults to C23**, under which `void init_uart();` means the
   function takes *zero* arguments — breaking the boot ROM's actual
   two-argument call to it. This one **hides**: the boot ROM's own `clean`
   target never removes stale `.o` files, so a fixed source tree can still
   fail until something invalidates the object cache. If you ever see this
   error after supposedly patching a checkout, check for a stale `.o` first.
3. **Broken git symlinks on a Windows-mounted checkout.** `core.symlinks`
   defaults to `false` there, so git-tracked symlinks (a device-tree source,
   a couple of vendored libs) materialize as plain text files *containing
   their own target path* instead of real symlinks. Invisible on a 1×1 build
   (nothing opens those paths); a multi-tile build's baremetal bootrom does,
   and `dtc` chokes trying to parse a path string as a device tree.
4. **CRLF-terminated shebangs**, 66 files, mostly inside the Ariane submodule.
   `#!/usr/bin/env python3\r` makes `env` look for a program literally named
   `python3\r`. Swept fixed checkout-wide.

Docker was built, then dropped — Docker Desktop itself proved unstable on the
development machine (a real WSL integration crash), and turned out to be
unnecessary: CHIA's node types are container-optional, and the bare-VM
`setup_commands` pattern (used for the GCP worker too) is simpler and is the
validated path. `dockerfiles/openpiton-chia.Dockerfile` still exists but
nothing depends on it.

### Phase 2 — the loop: complete, all 11 build steps landed

Each step shipped as its own small, tested, individually-committed change,
TDD throughout. The three gaps identified after the first pass are closed:

- **Cost tracking** — `Budget.max_usd` is enforced before each iteration, the
  same way `max_wall_s` already was. The dollar figure is a documented lower
  bound (per-task execution cost only, not the Planner's or triage's own call
  cost).
- **Checksum verification** — `verify_checksums()` runs before any LLM call
  or checkout is touched; a mismatch stops the run immediately with
  `status="checksum_mismatch"`.
- **Replay tagging** — `run_mace_loop` threads its own `run_id`/`iteration`
  through `integrate_parallel`, so a real run is tagged for replay
  automatically with zero extra caller effort.

The loop has completed multiple real end-to-end runs against real Ariane
hardware via `examples/mace_end_to_end.py`, recorded in
`runs/mace_end_to_end.db`: four runs, all `status=passed`, wall times ranging
roughly 1835s–2180s, task counts 1/5/1/2 across the runs — the varying task
count is the Planner genuinely deciding different decompositions for the same
nominal objective, not noise.

**One honest gap worth knowing about:** the mechanism for detect-failure →
diagnose → replan → eventually-pass is real and proven at tier 1 (a stateful
stub `sims` that fails its first run, passes after). It is *not* separately
documented as having happened end-to-end against **real hardware** — i.e., a
real Verilator run actually failing, getting triaged, and a subsequent real
run passing as a direct result. This is very likely achievable (gate
workloads were deliberately designed to fail on naive configs, and the
mechanism itself is proven), but if you want to strengthen the project's
evidence, deliberately provoking and capturing this on real hardware — rather
than assuming the four passing runs above did this incidentally — is
genuine, well-scoped, valuable work.

### The PicoRV32 extension (§7 in the paper)

Motivated by a bigger ask — could MACE take arbitrary core RTL and a spec and
integrate a new core into OpenPiton automatically? Investigated first, before
writing code: no, not as a general capability, because (as §2 above explains)
there's no reusable core-to-NoC bridge at all — every core needs its own
bespoke L15 adapter written by hand. The real dividing line turned out to be
"does the core have a coherent cache of its own" (Ariane's situation — would
need genuine new coherence-adapter RTL, out of scope) versus "does it not"
(PicoRV32's situation — a complete adapter already exists upstream, unused).

So the scoped-down, actually-shipped feature: extend `chia_openpiton`'s
supported-core list from `{ariane, sparc}` to `{ariane, sparc, pico}`. This
landed cleanly — a `Literal` entry, two `sims` flags
(`state_def.py`'s `sims_flags()`), mirrored tests, zero regressions to the
existing cores. The genuinely novel part: OpenPiton's own CI **builds**
PicoRV32 under Verilator but has **never run it** — the run job is commented
out and targets a nonexistent stage. This project's build passes cleanly (57
seconds — structurally lighter than Ariane's, since pico's config doesn't
pull in the bootrom/device-tree chain that fixes 2–4 above exist for). The
run reaches verdict `maxcycles` — see the next section for why this is a real
finding, not a bug to fix here.

**Note for whoever touches the loop driver next:** `examples/mace_end_to_end.py`'s
own `--core` argparse choices are still hardcoded to `("ariane", "sparc")` —
the adapter supports `pico` now, but the example script's CLI was never
updated to expose it. Small, real, easy fix if you want it.

### Two RTL-level findings, precisely characterized, deliberately not chased further

Both the 2×2 Ariane mesh and the 1×1 PicoRV32 run show the **identical
signature**: the generic OpenPiton boot/reset/IOB-handshake sequence
completes exactly as it does in a known-good passing run (compared directly,
line for line, against `chia_openpiton/test/fixtures/run_pass_sim.log`) — and
then the core simply never reaches its own trap address. No crash, no error,
just silence until the cycle budget runs out.

This was **not** accepted at face value as "another environment bug." Real
verification was done first: the compiled binary's symbol table and entry
point were checked directly (`objdump -t`/`-f`) against the run's own
configured trap addresses and `symbol.tbl` — both match exactly in both
cases. That rules out a bad build or a toolchain mismatch. What's left is
that the core itself, after a verifiably correct boot, never gets to its own
code.

Why this reads as a genuine RTL gap rather than an adapter bug: OpenPiton's
own diagnostic lists show **2×2 has no upstream Verilator precedent at any
tile count other than 1×1 and 4×4** — nobody has ever validated that
particular mesh shape. And **nobody has ever run PicoRV32 under any simulator
before**, so a first-ever attempt surfacing a first-ever bug in an
unvalidated configuration is exactly what you'd expect, not a surprise.

Neither was chased to a waveform-level root cause — that's a materially
deeper investigation (would mean tracing the reset/execution sequence signal
by signal) than anything else fixed in this project, which has otherwise all
been real-but-shallower environment and toolchain friction. If you want to
pick this up: start by comparing a waveform dump (`+trace` / FST output, if
enabled in the build) of the failing run against what you'd expect from the
reset sequence description in OpenPiton's own tile RTL, hart by hart.

### 4×4 Ariane: never reached a verdict, two real causes now understood

4×4 is the *one* multi-tile mesh shape OpenPiton's own CI has actually
validated, so after 2×2's hang, the natural move was to try the shape known
to work. It never completed, for two independent, precisely diagnosed
reasons — not vague "the environment was flaky":

1. A genuine Ray-reported out-of-memory condition (22.33GB/23.47GB used) from
   unconstrained parallel `cc1plus` compilation of Verilator's generated C++
   for the larger 16-tile design. Fixed by setting `MAKEFLAGS=-j1` in the
   environment before the build subprocess spawns — GNU Make honors an
   inherited `MAKEFLAGS` for any `make` invocation that doesn't pass its own
   `-j`.
2. A **second, separate cause found later**, via direct `/proc` inspection of
   a live, still-running build: Verilator's own generated build step invokes
   `make -j` (an explicit, *bare*, unlimited `-j`) directly on its own command
   line — and an explicit command-line `-j` always overrides an inherited
   `MAKEFLAGS`, silently. So fix #1 is real and correct, but doesn't reach
   this specific invocation.

If you want a real 4×4 datapoint for the paper's baseline (a): the fix is to
find where `sims` invokes Verilator's build (search for where it shells out
to `verilator --build` or the generated `make` call) and force a real,
non-overridable `-j1` there — a Makefile-level `MAKEFLAGS := -j1`
override, or dropping `--build`'s own implicit parallelism, would both work.
This is scoped, understood, and genuinely achievable — it just hadn't been
done yet as of this writing, because baseline/paper work took priority.

### Baselines and the paper

- **(a) manual mesh scaling** — 1×1 proven (repeatedly), 2×2 real
  build-pass/run-hang (above), no 4×4 datapoint (above).
- **(b) one-shot LLM, no tools, no iteration** — done,
  `examples/baseline_one_shot_llm.py`, real result: 229.1s total, passed on
  the first try (the LLM's guess happened to reach the same proven default
  config MACE itself converges to).
- **(c) full MACE loop** — done, reused the four real end-to-end runs
  described above (same objective, so no new run was needed).

The paper (`paper/mace_paper.tex` / `.pdf`) is drafted, typeset, and exactly 4
pages, covering all of the above honestly — including (b) beating (c) on
wall-clock for this specific easy objective, discussed directly rather than
hidden. Two things still worth doing before actual submission: the author
byline is currently a placeholder ("MACE Team"), and there's no architecture
figure (the pipeline is presented as text only).

## 8. Standing conventions — follow these, don't second-guess them

- **No AI co-author trailers in commits.** A deliberate, repeated instruction
  from the project owner, despite this being AI-assisted work throughout.
  Keep following it.
- **Small, focused commits, directly on `main`.** No branches, no PRs. The
  git log (`git log --stat`) is a long, linear, individually-readable chain —
  worth reading directly for context this doc doesn't repeat.
- **`chia_openpiton/` never imports `mace/`.** Checked by an actual test.
  `examples/` is the only place that mixes them.
- **An unclaimed file: `examples/run_barrier_atomic.py`.** It's
  well-formed and functional-looking (a direct, LLM-free
  configure→build→run driver for the barrier_atomic workload), but nobody
  has claimed authorship, and it's never been committed. Left alone rather
  than deleted or committed on someone else's behalf, the whole project. If
  you know what it is, either claim it (commit it, with a real message) or
  ask before removing it.
- **The OpenPiton checkout itself carries uncommitted state** — the pyHP
  preprocessor's `.tmp.v` build byproducts get written back into the source
  tree on every build. Expected, long-standing, not a sign something's
  broken; don't be alarmed by `git status` inside that checkout.
- **Build/output artifacts are gitignored, not missing**: `mace.egg-info/`,
  `runs/`, `.pytest_cache/`, `*.db`, `.env` (site-specific values — GCP
  project, paths, auth keys — ask the project owner rather than trying to
  reconstruct it), and the LaTeX build junk under `paper/`.
- **Ownership.** An earlier planning document assigned a specific split
  (adapter/hardware-facing work vs. cluster/LLM/loop-driver work) between two
  people, but the git history shows one author throughout to date — confirm
  the actual current division of labor directly with the project owner
  rather than assuming the original split still holds.

## 9. Open problems — genuinely scoped things you could pick up

Ordered roughly by value-per-effort, not urgency — none of these block the
paper or the core submission, which stand on their own already.

1. **Waiting on [ucb-bar/chia#72](https://github.com/ucb-bar/chia/issues/72).**
   A real CHIA-side bug (task leases from the GCS never reach a
   tailnet-relayed worker's raylet, even though that worker's own outbound
   registration/heartbeat works fine — confirmed via a direct raylet
   state-dump during a patient, six-minute-budget retest, not a guess) is
   filed upstream. If you're curious and have GCP time to spend, the next
   real step is packet-level tracing on both the head's relay process and
   the GCP worker's raylet to see whether a lease-assignment RPC is sent and
   lost, or never sent at all — described precisely in the issue itself.
2. **A real 4×4 fix** — §7 above has the exact diagnosis; forcing a real
   `-j1` into Verilator's own generated build-step `make` invocation (not
   just the environment) would likely get an actual 4×4 pass/fail verdict,
   strengthening baseline (a).
3. **Waveform-level tracing of the 2×2/pico hangs** — genuinely open, and
   genuinely deeper work than anything else in this project so far. Start
   from the exact divergence point described in §7: boot/reset/IOB completes
   identically to a passing run, then the core never traps.
4. **`examples/mace_end_to_end.py`'s `--core` choices don't include `"pico"`**
   yet, even though the adapter supports it. Small, real, quick fix.
5. **A real hardware proof of the fail→triage→replan→pass cycle** (§7's
   "one honest gap") — deliberately provoke a real gate failure on real
   hardware and let the loop recover from it, rather than relying on the
   four existing runs which may not have exercised this path.
6. **The mystery file**, `examples/run_barrier_atomic.py` — needs a human
   decision, not more investigation.
7. **Paper polish** — real author names/affiliations, an architecture figure.
8. **`docs/api/openpiton.rst`** — only matters if actually filing a PR to
   upstream CHIA; the checklist for that is in
   [`chia_openpiton/README.md`](../chia_openpiton/README.md)'s last section.

## 10. Where to find more

- [`README.md`](../README.md) — quickstart, install, the four ways to run
  MACE.
- [`chia_openpiton/README.md`](../chia_openpiton/README.md) — the adapter's
  own docs: worker requirements, every gotcha with its mechanism explained,
  the checkout-location requirement, the upstreaming checklist. Kept
  self-contained on purpose, so it can move into a CHIA PR unchanged.
- [`paper/mace_paper.pdf`](../paper/mace_paper.pdf) — the full 4-page
  write-up: architecture, results, baselines, and every limitation stated as
  precisely as this doc states them.
- [`docs/chia_tailnet_issue_draft.md`](chia_tailnet_issue_draft.md) — the
  text that became [ucb-bar/chia#72](https://github.com/ucb-bar/chia/issues/72).
- [`docs/PROJECT_HANDOFF.pdf`](PROJECT_HANDOFF.pdf) — an earlier
  point-in-time snapshot (Sep 8) with a very detailed blow-by-blow of the GCP
  investigation as it was happening live; superseded by this doc for current
  status, but worth reading if you want the full narrative of how the GCP
  finding was reached, including a wrong turn that was caught and corrected.
- `CHIA_proposal.pdf` — the original hackathon proposal.
