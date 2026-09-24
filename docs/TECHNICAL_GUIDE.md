# MACE Technical Guide

This is the deep-dive doc — written for a teammate picking up this project,
not just running it. It explains what's here, why it's built the way it is,
what's actually proven versus hoped, and what's genuinely left to do. Where
something is a CHIA or OpenPiton concept you might not already know, it's
explained here rather than assumed — that's deliberate, so you can pick this
up without a separate crash course.

For a quick "how do I run this" without the depth, see [`README.md`](../README.md)
instead — Section 9 and Section 10 below cover the same ground plus GCP clustering setup
and a full walkthrough, in more depth. This doc is the one to actually read
start to end.

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
one whole checkout," not "one CPU core" or anything generic — see Section 4 below
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
  extension (Section 7). Has no cache of its own (non-coherent), so its L15 adapter
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
  dockerfiles/       A worker image — built once, not currently used (Section 8).
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
| `orchestrator.py` | `run_mace_loop()` — the top-level entry point. See the call chain in Section 5. |
| `metrics.py` | `SQLiteNode`-backed record of runs/iterations/tasks/failures. `summary()` derives the five metrics the proposal promises. |
| `llm.py` | `make_llm()` picks a backend from the `MACE_LLM` env var (`opencode` default, or `claude`/`antigravity`/`vertex`). `extract_cost_usd()` reads a per-call dollar cost where the backend's response object carries it after a remote round-trip — always `0.0` for Claude, a documented real API-shape limitation (its cost lives on the LLM instance's own local state, invisible once dispatched remotely), not a bug silently swallowed. |
| `replay.py` | Wraps CHIA's cache/bypass mechanism (see Section 2). Scoped honestly around its real limit — decisions only, never side effects. |
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
| 2 | 2×2 Ariane build on a real GCP worker | **not yet green** — the dispatch blocker is resolved (Section 9), but the build itself hit a worker OOM/crash before reaching a verdict |
| 3 | Fan-out: parallel builds across two checkouts | **real hardware, proven** — 176s vs 324s serial |
| 4 | `chia viz` renders the example's task graph | proven |
| 5 | Tier-0 suite green on fixtures | proven, currently green |

Five real, non-obvious bugs were found only by running the actual toolchain,
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
5. **`my_top.cpp` never called Verilator's coverage-write API**, so even a
   run that completed cleanly produced no `coverage.dat`. Fixed by adding
   the call, guarded by `#if VM_COVERAGE` — note `#if`, not `#ifdef`:
   Verilator's generated Makefile always defines that macro to 0 or 1,
   never leaves it undefined, so an `#ifdef` guard (an earlier version of
   this fix used one) is always true regardless of value. That bug broke
   every plain, non-coverage build's link step
   ("undefined reference to `VerilatedCov::...`"), unrelated to whether
   coverage was ever requested for that build.

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
`runs/mace_end_to_end.db`: seven runs now, all `status=passed`, wall times
ranging roughly 1795s–3832s, task counts 1/5/1/2/2/5/5 across the runs — the
varying task count is the Planner genuinely deciding different
decompositions for the same nominal objective, not noise. Runs 5–7 are new
evidence of a different kind: run 5 targeted `producer_consumer.c` (never
previously run through the loop; the first four all used `barrier_atomic.c`)
and passed cleanly; runs 6–7 targeted `scatter_gather.c` under deliberately
harder L1D cache configurations (see the "one honest gap" note below) and
also passed. Real confirmation the loop generalizes across gate workloads
and across non-default configurations, not just repeatedly succeeding on one
proven combination.

**One honest gap that's still open, now checked four ways:** the mechanism
for detect-failure → diagnose → replan → eventually-pass is real and proven
at tier 1 (a stateful stub `sims` that fails its first run, passes after).
It is *not* documented as having happened end-to-end against **real
hardware**. Four deliberate, increasingly aggressive attempts to provoke it
all instead passed cleanly on the first try: an unfamiliar workload
(`producer_consumer.c`, then `scatter_gather.c`), an LLM-chosen reduced L1D
cache, and finally a mandatory, explicit extreme L1D geometry (128 bytes,
direct-mapped — one cache line) that both the human prompt and the planning
agent's own task description explicitly predicted would fail. Verified via
the real `sims` invocation line (not the agent's self-report) that this
extreme value was genuinely used, not silently softened. It still passed.
The `failures` table is confirmed completely empty across all seven real
runs, checked directly against the database. Our read: this is a genuine,
interesting finding about this RTL's coherence-protocol robustness to
cache-capacity extremes, not a failed test design — see Section 11 item 5 for
where a fifth attempt would need to look (mesh/NoC parameters, not cache
geometry, since that lever now looks exhausted).

**Update, as of 2026-09-20 -- both counts above are now stale, since this is
a live, appended-to local database, not a fixed artifact:**
`runs/mace_end_to_end.db` has 14 runs total (7 more since the above was
written) -- 8 `passed`, 4 stuck `running`, 1 `failed`, 1 `budget_exceeded` --
and the `failures` table is no longer empty (3 rows, all from the
`budget_exceeded` run: `missing_toolchain` / "the risc-v gnu toolchain is
not installed or its executables are not accessible via the build
environment's path", none `recovered`). This is a real infrastructure
failure, not a real hardware RTL one, so it does **not** close the "one
honest gap" above -- but the specific "completely empty" claim itself is no
longer accurate and should not be quoted as current.

### The PicoRV32 extension (Section 5 in the paper)

Motivated by a bigger ask — could MACE take arbitrary core RTL and a spec and
integrate a new core into OpenPiton automatically? Investigated first, before
writing code: no, not as a general capability, because (as Section 2 above explains)
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
pull in the bootrom/device-tree chain that fixes 2–4 above exist for).

**Update, superseding the rest of this section as originally written:** the
run initially reached verdict `maxcycles` (see below for why that read as a
real finding, not an environment bug) -- but unlike 2×2 Ariane, this one
*was* chased to a waveform-level root cause, and pico now genuinely
**passes** (`Simulation -> PASS (HIT GOOD TRAP)`), a first for this core
under any simulator, by anyone. Three real, independently waveform-verified
bugs, all fixed: picorv32's own `resetn`/`booted` self-boot gate
(`scripts/patch_openpiton.sh` fix 6 -- it was waiting forever for an
interrupt nothing in a bare config ever sends), the manycore monitor's
`active_thread` tracking for pico's tile (fix 7 in the same script), and a
real-silicon BIST self-clear race silently discarding pico's first, very
early memory writes. See README.md's "What's proven, honestly" section for
the current, authoritative summary (this doc's own narrative below was
written before this was resolved, and is kept for its RTL-investigation
methodology, not its conclusion).

**Note for whoever touches the loop driver next:** `examples/mace_end_to_end.py`'s
own `--core` argparse choices are still hardcoded to `("ariane", "sparc")` —
the adapter supports `pico` now, but the example script's CLI was never
updated to expose it. Small, real, easy fix if you want it.

### Two RTL-level findings, precisely characterized — both now root-caused and fixed

Both the 2×2 Ariane mesh and the 1×1 PicoRV32 run originally showed the
**identical signature**: the generic OpenPiton boot/reset/IOB-handshake
sequence completes exactly as it does in a known-good passing run (compared
directly, line for line, against `chia_openpiton/test/fixtures/run_pass_sim.log`)
— and then the core simply never reaches its own trap address. No crash, no
error, just silence until the cycle budget runs out. Pico's own three real
RTL/testbench bugs are covered above. This section covers 2×2 Ariane, since
resolved to a real, verified pass.

**Update: chased to a full, verified root cause — two separate, real bugs,
both fixed, both committed locally in the checkout.**

1. The "only `trace_hart_00.dasm` exists" evidence that originally looked
   like "tiles 1-3 never boot" is a *different, unrelated* bug: `cva6.sv`'s
   Verilator mock-tracer `initial` block hardcodes the filename
   `trace_hart_00.dasm` regardless of `hart_id_i`, unlike the neighboring
   dromajo DPI calls in the same block (which correctly pass `hart_id_i`) and
   unlike `instr_tracer.sv`'s own `create_file()` (which does
   `$sformat(fn, "trace_hart_%0.0f.log", hart_id)`). Every tile's core opens
   the same file at time 0 with truncating semantics, so only whichever
   tile's `initial` block runs last ends up owning it — on *any* multi-tile
   Ariane build, independent of tile count. Fixed by parameterizing the
   filename the same way `instr_tracer.sv` does (commit
   `ariane: parameterize per-tile trace_hart_NN.dasm filename by hart_id_i`,
   in the `ariane` submodule). With this fix, all N tiles produce their own
   trace file, giving real per-tile evidence instead of a coincidental
   artifact.
2. The actual blocker: `piton/verif/diag/assembly/include/riscv/ariane/syscalls.c`
   (OpenPiton's own shared, upstream exit-barrier code, used by *every*
   ariane diagnostic, not just this project's gate workloads) polls
   `finish_sync0`/`finish_sync1` — both `volatile static uint32_t`, each
   bumped by another tile's `ATOMIC_OP` — with a **plain load**
   (`while (finish_sync0 != nc);`), not an atomic read. This is the exact
   same class of bug `mace/workloads/barrier_atomic.c` already documents and
   works around with its own `atomic_read()` helper (a plain volatile load
   right after another hart's atomic write does not reliably observe it on
   this RTL/toolchain combination) — just sitting in shared, upstream code
   nobody had exercised multi-tile before, since 1x1 (`nc=1`) always
   satisfies this barrier with a hart's own write, trivially, and no real
   ariane diagnostic had ever been run multi-tile until this project tried
   2×2. Fixed by mirroring the `atomic_read()` pattern into both polling
   loops in `syscalls.c` (same commit family, `ariane` submodule). Verified
   directly: a real, clean 2×2 Ariane `barrier_atomic.c` pass, all four
   tiles independently confirmed reaching `Hit Good trap`, in 58.9s (versus
   1401-1515s for every failing attempt before the fix).

Why the earlier "likely RTL gap, not chased further" framing was reasonable
at the time: OpenPiton's own diagnostic lists show 2×2 has no upstream
Verilator precedent at any tile count other than 1×1 and 4×4 — nobody had
ever validated that shape, so a first-ever attempt surfacing a first-ever bug
was expected, not a surprise. What changed is doing the actual waveform-free,
trace-file-and-source-reading investigation (the same discipline already
used for pico's own three bugs) rather than stopping at the "not chased
further" line.

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

**Update: cause 2 fixed, applied to the checkout, confirmed reproducible.**
`sims,2.0`'s own `vlt_build` step invoked a bare `make -j` (unlimited
parallelism) to compile Verilator's generated C++, and a command-line `-j`
always overrides an inherited `MAKEFLAGS`, no matter what it's set to — this
is why cause 1's own `MAKEFLAGS=-j1` fix never actually reached this
specific invocation. Fixed in `scripts/patch_openpiton.sh` (fix 9): the bare
`-j` is dropped from that one `make` invocation, so `MAKEFLAGS` from the
environment now genuinely controls it. Confirmed real: after this fix, a
4x4 build with `MAKEFLAGS=-j2` hit a *different*, previously-hidden race in
the bootrom's own Makefile (`startup.S` needs `rv64_platform.dtb`, which an
earlier parallel job's own cleanup step deletes before `startup.o` consumes
it — invisible under fully serial `-j1`, since there's no race to hit). The
practical, still-recommended setting remains `MAKEFLAGS=-j1` for a 4x4
build; a real fix for the bootrom race itself is a separate, small,
not-yet-done Makefile dependency-ordering fix.

**Update: 4x4 now genuinely passes, all 16 tiles independently confirmed —
after finding and fixing a third real bug, a hardcoded 32-bit monitor
register.** With causes 1 and 2 above fixed, a real 4x4 `barrier_atomic.c`
run reached `Simulation -> PASS`, but only 8 of 16 tiles' own trace files
(`trace_hart_N.dasm`, one per tile once the tracer-filename fix below was
also applied) showed the core actually reaching the `pass` label; tiles 8-15
were still executing unrelated code when the simulation declared victory.
Root cause: `piton/verif/env/manycore/pc_cmp.v.pyv` declares `finish_mask`
as a Verilog `integer` under `` `ifdef VERILATOR `` — always exactly 32 bits
per the language spec — while the sibling `active_thread`/`good` registers a
few lines below are correctly widened by this same template's own
`PITON_NUM_TILES*4-1` substitution. Reading `finish_mask` beyond bit 31
returns 0 regardless of what `-finish_mask=` string is passed, so
`good == finish_mask` was satisfied the moment the first 8 tiles (32 bits
÷ 4 thread-slots) finished, independent of the other 8. Fixed by giving
`finish_mask` the same template-widened declaration as its siblings
(committed locally in the checkout: `manycore monitor: widen finish_mask
under Verilator to match active_thread/good`). Verified directly: a clean
rebuild reached `PASS` with all 16 tiles' own trace files confirmed looping
at the real `pass` label (`0x80000540`), not just the aggregate verdict.

This was found alongside a second, unrelated real bug from the same
investigation: `piton/design/chip/tile/ariane/core/cva6.sv`'s Verilator
mock-tracer hardcoded `trace_hart_00.dasm` regardless of `hart_id_i`, so
every tile's core silently shared and overwrote the same file — the
original "only tile 0 has a trace" evidence that looked like "tiles 1-3
never boot" for the 2x2 case above. Fixed by parameterizing the filename
the same way `instr_tracer.sv` already does (`ariane` submodule commit:
`ariane: parameterize per-tile trace_hart_NN.dasm filename by hart_id_i`).

And a third, the actual reason 2x2/4x4 never passed at all before any of
this: OpenPiton's own shared, upstream `syscalls.c` (used by every ariane
diagnostic, not just this project's own gate workloads) polls its exit
barrier (`finish_sync0`/`finish_sync1`) with a plain load, not an atomic
read — the identical staleness bug `mace/workloads/barrier_atomic.c`
already documents and works around with its own `atomic_read()` helper,
just sitting in code nobody had exercised multi-tile before (1x1's `nc=1`
trivially satisfies this barrier with a hart's own write). Fixed by
mirroring `atomic_read()` into both polling loops (same `ariane` submodule
commit family).

**Update (2026-09-24): `syscalls.c` was never the bug; the atomic polling
is a workaround.** The root cause is a Verilator 5 scheduling bug,
[verilator#5829](https://github.com/verilator/verilator/issues/5829)
(partial assignments to one packed struct across processes get the wrong
evaluation order). CVA6's `core/cache_subsystem/wt_l15_adapter.sv` hits it
exactly: `dcache_rtrn_o.inv.vld/.all` come from continuous `assign`s while
`p_rtrn_logic` reads them to raise `dcache_rtrn_vld_o`, so invalidations
from the coherence network are sometimes dropped and the L1D keeps stale
lines. Plain loads then miss other tiles' updates, while atomics (served at
L2) see them. Upstream CVA6 fixed this on 2025-03-06 with
[cva6#2809](https://github.com/openhwgroup/cva6/pull/2809) (commit
`c511b2191`, moves those four fields into the always block), but OpenPiton
pins CVA6 at `4c01614f8` (2022-10-13), which predates it.

A/B in `/home/potato/openpiton-b`, upstream plain-load `syscalls.c`,
Verilator 5.020 `--no-timing`, clean 2x2 build, `barrier_atomic.c`, with
only the adapter differing:

| CVA6 adapter | result |
|---|---|
| cva6#2809 ported (4 lines moved into `p_rtrn_logic`) | pass, 4 of 4 tiles, 24s |
| original, as OpenPiton pins it | timeout, 1 of 4 tiles, 431s |

The upstream patch does not apply cleanly to the 2021-era file, so it was
ported by hand: add the four `icache_rtrn_o/dcache_rtrn_o.inv.vld/.all`
assignments to the top of `p_rtrn_logic` and delete their four `assign`
lines. `openpiton-b` is back on the original adapter. The published 2x2/4x4
results used the `syscalls.c` workaround (patch fix 11), which remains in
`scripts/patch_openpiton.sh` so those numbers reproduce exactly. The
proper fix is porting cva6#2809, which also likely explains the gate
workloads' own `atomic_read()` requirement.

Final, real, independently verified numbers: 2x2 Ariane passes in 58.9s,
4x4 in 503.1s, both confirmed tile by tile via each tile's own execution
trace, not just the monitor's aggregate `PASS` message.

Those two numbers are direct adapter runs (`OpenPitonWorkspaceNode.build()`/
`run()` called by hand), simulation time only, not the MACE loop. The
earlier "2x2/4x4 loop passes" were really 1x1 runs: `examples/mace_end_to_end.py`
hardcoded `target_mesh=(1, 1)`, and the objective text never sizes a build.
It now takes `--mesh`, and the full loop was run at both sizes on
2026-09-23/24 (all on the patched `/home/potato/openpiton`, one at a time):

| run_id | core, mesh | result | notes |
|---|---|---|---|
| `9f1d0932cb6d` | ariane 2x2 | passed, 750.1s, 4/4 tasks | planner also built and verified an 8-way L1D variant |
| `f8a01774f7a0` | ariane 4x4 | crashed | all six 16-tile sims passed; Vertex then rejected a `unit_test` task's tool list (see below) |
| `fdd6c4322b17` | ariane 4x4 | passed, 250.9s, 2/2 tasks | rerun after the fix, cached builds |
| `fd0c370667aa` | pico 2x2 `addi.S` | passed, 70.7s, 1 task | |
| `c2d68cc5bef7` | pico 4x4 `addi.S` | budget_exceeded, 3 iterations | stale cached build (see below); triage blamed the RTL each time |
| `4c9768c9a30a` | pico 4x4 `addi.S` | passed, 293.2s, 1 task | after moving the stale build aside; includes the forced rebuild |
| `d7c5ccaee522` | pico 4x4 `addi.S` | passed, 100.6s, 1 task | warm rerun on the rebuilt cache (~16s of it is simulation) |

Every pass was checked tile by tile in its `sim.log`: one `Hit Good trap`
per tile, and a full-width `finish_mask`. Two real bugs surfaced:

- **Tool-name collision.** CHIA's API backends name each tool function
  `f"{tool}__{fn}"[:64]`, and `TestbenchEditTool`'s functions are named
  `f"{tool}_{method}"` with the tool named after the planner's task id. A
  task id like `T6_UnitTestCoherenceLogic` truncated all three functions to
  one name, and Vertex rejected the request ("Duplicate function
  declaration found"). `mace/loop.py` now names the tool from a short hash
  of the task id (`_unit_test_tool_name`, with a regression test).
- **Stale cached build.** The pico 4x4 build (`mace_d8be38b3186c`) was
  compiled before the `finish_mask` fix. A build ID covers the
  configuration, not source edits, so the loop reused it: its 32-bit mask
  never matched once all 16 pico tiles finished together, the absent
  threads 1-3 timed out, and all three replans hit the same build. It was
  moved aside (not deleted) to
  `build/manycore/mace_d8be38b3186c.stale-pre-finish-mask-fix`; the clean
  rebuild passed. Any build from before a monitor/RTL fix needs the same
  treatment (`clean=True`, or move the build directory aside).

The same 4x4 run's planner also asked for `CONFIG_RTL: ... |
CONFIG_ENABLE_MESH_ATOMIC_FIX`, a define that appears nowhere in OpenPiton:
harmless, but it cost a full extra 4x4 build, since nothing validates
`CONFIG_RTL` flags against the RTL yet.

### Three-way comparison and cost (2026-09-24, the paper's Table 1)

End-to-end wall time, all approaches sharing one build cache (a
configuration built earlier is reused; an approach pays for a build only
when it picks a new one). Manual (a) is machine time only, measured today
by building (cached) and running the default config through the adapter by
hand. The paper reports only the multi-tile meshes: ariane runs
`barrier_atomic.c`, pico runs `addi.S` with the BIST define named in every
approach's objective, and sparc is left out (it does not run under
Verilator 5, see the sparc section).

| core, mesh | (a) manual | (b) one-shot | (c) MACE loop |
|---|---|---|---|
| ariane 2x2 | pass 33.9s | fail 10.4s (l15_size=0) | pass 750.1s |
| ariane 4x4 | pass 271.9s | fail 3820.9s | pass 250.9s |
| pico 2x2 | pass 9.8s | fail 12.0s (l15_size=0) | pass 70.7s |
| pico 4x4 | pass 53.9s | fail 13.4s (l15_size=0) | pass 100.6s |

Pico's fix is an RTL define, so the one-shot `CONFIG:` line gained an
optional `config_rtl` field before the pico (b) runs (the ariane (b) runs
predate it and need no define). Both pico one-shots included
`CONFIG_DISABLE_BIST_CLEAR` and still zeroed the L1.5. An earlier pico 4x4
one-shot without the field picked valid caches but could not request the
define; it was stopped mid-build. Logs: `runs/baseline_a_pico.log`,
`runs/baseline_b_pico_{2x2,4x4}_rtl.log`, `runs/loop_pico_4x4_warm.log`.

The earlier single-tile record, kept here but no longer in the paper:

| approach | ariane 1x1 | ariane 2x2 | ariane 4x4 | sparc 1x1 | pico 1x1 |
|---|---|---|---|---|---|
| (a) manual | pass 6.4s | pass 33.9s | pass 271.9s | fail 13.9s | fail 18.2s |
| (b) one-shot | fail 186.5s | fail 10.4s (l15_size=0) | fail 3820.9s | fail 766.0s | fail 32.6s (l15_size=0) |
| (c) MACE loop | pass 741.7s | pass 750.1s | pass 250.9s | budget 966.3s | budget 1250.5s |

One-shot 4x4 (`runs/baseline_b_4x4.log`) chose L1 32KB/4, L1.5 256KB/8, L2
4MB/8: a 2079s build, then `FAIL(TIMEOUT)` after 1729s of simulation with no
tile finished. Logs: `runs/baseline_a_manual.log`, `runs/baseline_b_{2x2,4x4}.log`.

Per-run cost at 2x2 (Gemini 2.5 Flash at $0.30/$2.50 per M input/output
tokens, thinking billed as output; compute as `e2-highmem-8` at $0.36/hour
over the run's wall time): ariane ~$0.12 ($0.043 LLM + $0.075 compute),
pico ~$0.02, a failing sparc run an estimated ~$0.25. Tokens were measured
by re-sending the planner and task prompts once, because the recorded
`compute_usd` is always 0: `VertexGeminiLLM` returns no cost, and its token
counter (`candidates_token_count`) skips `thoughts_token_count`, which was
more than half of the billed output.

### Baselines and the paper

- **(a) manual mesh scaling** — 1×1 proven (repeatedly), 2×2 real
  build-pass/run-hang (above), no 4×4 datapoint (above).
- **(b) one-shot LLM, no tools, no iteration** — done,
  `examples/baseline_one_shot_llm.py`. The "229.1s, passed" number once
  recorded here predates the rtl_timeout fix (Section 7's own item, and the same
  bug the codebase review's baseline-comparison finding named) and is
  stale -- see README.md's own baseline section for the current, real
  result and framing.
- **(c) full MACE loop** — done. A fresh same-day run under the current
  code (Section 7's earlier four end-to-end runs predate the orchestrator
  wall-time fix, so aren't directly comparable to this one) took 741.7s
  and passed, 4/4 tasks, one iteration -- see README.md.

The paper (`paper/mace_paper.tex` / `.pdf`) is typeset at exactly 4 pages.
Figure 1 is the author's draw.io pipeline diagram
(`paper/mace_figure1.drawio.svg`, editable in draw.io), rendered to
`paper/mace_figure1.pdf` with headless Chrome in the light color scheme:
its labels are HTML `foreignObject`s, which SVG-to-PDF converters drop.
Table 1 is the 2x2/4x4 three-way comparison above.

### The full 3-core × 2-baseline matrix

Everything above is Ariane only. The same (b)/(c) pair was also run for real
against `sparc` and `pico` -- same objective (1×1 mesh, `barrier_atomic.c`),
same LLM backend (Gemini 2.5 Flash on Vertex) -- completing a genuine 3-core
× 2-baseline matrix. This wall-time data is now in the paper too (Figure 2,
left), alongside a second new figure comparing manual mesh scaling against
the full loop on 2×2/4×4 (Figure 2, right) -- both real `pgfplots`/TikZ
figures added when the paper was revised with real charts and diagrams,
still fitting the exact 4-page limit. The fuller per-core discussion below
is recorded here instead, in more depth than the paper's own tighter prose
has room for. Raw logs: `runs/bench_{sparc,pico}_{b,c}.log`; the (c) runs
are also in `runs/mace_end_to_end.db` (run IDs `e91cc625501a` sparc,
`fc4309c57074` pico).

| Core     | (b) one-shot LLM                                                          | (c) full MACE loop                                        |
|----------|-----------------------------------------------------------------------------|--------------------------------------------------------------|
| ariane   | fail, 186.5s -- built, failed hardware verification                       | pass, 4/4 tasks, 1 iteration, 741.7s                          |
| sparc    | fail, 766.0s -- built, but the run command itself failed (no verdict)      | `budget_exceeded`, 0 tasks passed, 3 iterations, 966.3s       |
| pico     | fail, ~33s -- LLM's own config was unparseable (`l15_size=0`), rejected before any build | `budget_exceeded`, 0 tasks passed, 3 iterations, 1250.5s      |

Neither sparc nor pico passed under either baseline here -- but the *why*
differs sharply between them, and it's the full loop's own triage/post-mortem
machinery (not us, reading logs by hand) that told the two apart, which
neither baseline (b) nor a bare pass/fail number could show on its own:

- **sparc (c):** the config task's own Verilator model build succeeded, but
  every one of 3 replan attempts still failed to *run* the gate workload --
  `command failed (rc=1)`, no verdict. The loop's own triage diagnosed a
  missing `util.h` include path in the diagnostic program's own build and
  its post-mortem classified the whole run `fixable_config`, not a hardware
  limitation.

  **Update: independently verified, and the LLM's own diagnosis was wrong.**
  This is not a missing include path -- it is a genuine ISA incompatibility
  in `barrier_atomic.c` itself. Its `atomic_read()` helper calls
  `util.h`'s `ATOMIC_FETCH_OP`/`ATOMIC_OP` macros, which expand to literal
  RISC-V inline assembly (`amo<op>.<type>`, an AMO instruction) --
  confirmed by reading
  `piton/verif/diag/assembly/include/riscv/ariane/util.h` directly. OpenSPARC
  T1 (this project's `sparc` core) has no RISC-V instructions at all, and
  OpenPiton's own sparc diag suite
  (`piton/verif/diag/assembly/include/`) is entirely assembly-based --
  no C diag environment exists for sparc anywhere in the checkout, so there
  is no portable path this include could have taken. A `find` across the
  whole checkout for a sparc equivalent of these macros comes back empty.
  Reported here as an honest, precisely-characterized limitation rather than
  something to fix: fixing it would mean writing new SPARC assembly, out of
  scope for an adapter/orchestration project like MACE.
- **pico (c):** every one of 3 replan attempts reached verdict `maxcycles`
  (deadlock) on the 1×1 mesh. The loop's own post-mortem reasoned that
  `barrier_atomic.c`'s own barrier logic needs more than one participant to
  ever release -- something a 1×1 mesh can never provide, no matter how the
  config is retried -- and classified this `likely_hardware_limitation`.
  More precisely this is a workload/mesh mismatch, not an RTL bug: pico's
  own adapter already passes for real (see the update above) on a workload
  shaped for a single core. This is a *different* failure from the earlier,
  now-fixed pico boot/trap RTL bugs -- those were already fixed before this
  run, and this run's failure is about workload choice, not a regression.

  **Update: the full loop now passes on pico for real, on a compatible
  workload.** `barrier_atomic.c` still cannot pass on a 1×1 pico mesh (the
  mismatch above is real and unrelated to what follows) -- but re-running
  the loop against `addi.S` (the single-hart diagnostic already proven to
  pass by hand, see the earlier update) gets a genuine pass, this time
  driven by the Planner itself rather than a hand-built config. That needed
  one real new feature: a `CONFIG_RTL:` planner directive (mirroring the
  existing `CACHES:` one) so a task can ask for extra RTL defines --
  `CONFIG_DISABLE_BIST_CLEAR` here -- on top of the mesh's defaults; before
  this, the Planner had no way to express that fix at all. The fix was named
  directly in the run's own objective text -- this demonstrates the Planner
  *applying* a known fix when told what it is, not discovering it from
  scratch, which generic triage alone still cannot do. Given that hint, the
  Planner correctly emitted `CONFIG_RTL: pico_addi_1x1 |
  CONFIG_DISABLE_BIST_CLEAR`, and the run passed: `status=passed`, verdict
  `pass` (`Simulation -> PASS (HIT GOOD TRAP)`, cycle 3279750), one task, one
  iteration, 115.5s (run ID `439cb52d67e5` in `runs/mace_end_to_end.db`).
  This is PicoRV32's second real Verilator pass ever, and the first driven
  by MACE's own loop rather than a standalone script.

  One gotcha surfaced getting here: an earlier attempt split the work into a
  `config` task plus a dependent `workload` task, and attached `CONFIG_RTL:`
  only to the `config` task's id. Each task in a MACE DAG builds against its
  own independently-computed `PitonConfig` (`mace.loop._config_for_task`) --
  there is no inheritance from a dependency's config -- so the `config`
  task's own smoke-test run passed while the dependent `workload` task
  rebuilt with the default RTL defines and timed out, and the run reported
  `budget_exceeded` despite the fix being correctly applied to one task. The
  fix was to ask for a single task (the loop already builds and runs any
  task, `config` or `workload` alike -- the kind label carries no structural
  difference), not to patch around the per-task independence, since that
  independence is also what lets two unrelated tasks use different cache
  geometries in the same run.

  **Update: the boot fix generalizes to larger meshes; `barrier_atomic.c`
  never runs on pico at any mesh size, for a simpler and more fundamental
  reason than first suspected.** On a 2x2 pico mesh, all four tiles
  independently reach `Hit Good trap` on `addi.S` (`Simulation -> PASS`,
  cycle 5179750) -- the boot fix holds at scale, confirmed at 4x4 too.
  Switching to `barrier_atomic.c` on the same meshes, now with multiple real
  participants available (unlike the 1x1 case above), still does not pass:
  every core's PC advances through the reset vector once, then never moves
  again. This first looked like a cross-tile atomic/coherence gap, since the
  symptom is identical to what genuine cross-tile bugs looked like elsewhere
  this session -- but a fresh diagnostic pass found the real cause is much
  simpler: pico's OpenPiton integration has **no C compiler at all**, only
  an assembler (`piton/tools/bin/rv32_as`) for `.S`/assembly sources. There
  is no `rv32_cc`, and `piton/verif/diag/assembly/include/riscv/pico/` has
  no `crt.S`/`syscalls.c` (both exist for `riscv/ariane`). `barrier_atomic.c`
  is a `.c` file, so it never compiles -- confirmed directly: `rv32_as.log`
  shows `cc1: fatal error: diag.S: No such file or directory`, and the
  resulting `mem.image` is empty (2 lines, versus 34 for a real `addi.S`
  build). Every tile boots into an empty memory image and idles at its reset
  vector; this is a toolchain gap, not an RTL bug, and not a return of the
  workload/mesh-mismatch framing above (that framing was itself never
  independently verified and turns out to describe the wrong mechanism).
  Building real `rv32_cc`/`crt.S`/`syscalls.c` support for pico was
  considered and explicitly scoped out as substantial new engineering with
  its own real risk (no template to copy verbatim for PicoRV32's own
  boot/CSR conventions). Instead, pico's actual AMO/atomic hardware path was
  verified directly and far more cheaply: the upstream `amoadd_w.S`
  architecture test (pure assembly, no C dependency, part of OpenPiton's own
  RV32 riscv-tests suite already in the checkout) passes for real on pico
  (`Simulation -> PASS`), confirming the atomic-memory-operation logic
  `barrier_atomic.c` would need is functionally correct at the instruction
  level, without needing to build and trust an entirely new, untested
  compilation path just to prove it.

- **sparc, a second finding beyond the ISA gap above: the core never
  wakes up.** Pure-assembly diagnostics (which never touch `util.h`'s
  RISC-V-only macros) still hang, including `princeton-test-test.s`,
  OpenPiton's own CI test for sparc at 1x1 (`.gitlab-ci.yml`). The I/O
  bridge model (`ciop_iob.v.pyv`) does send the power-on wake-up interrupt
  ("IOB sending to tile X: 0 Y: 0", CPX packet `0x17...10001`), but the core
  never receives it: `cmp_pcxandcpx.v` never prints "received interrupt
  vector", and the sparc pipe monitor shows thread 0 idle at PC 0 for the
  whole run. The diag itself is fine (non-empty `mem.image`, clean
  `midas.log`).

  An earlier session "fixed" this by forcing `active_thread` on for all four
  sparc threads in `pc_cmp.v.pyv`'s `RTL_SPARC0` branch (committed locally,
  then shipped as `patch_openpiton.sh` fix 10). That was a misdiagnosis,
  now reverted in both places: upstream deliberately leaves that branch
  empty, because `cmp_pcxandcpx.v` sets a sparc thread's `active_thread` bit
  only when its reset interrupt (`INT_RET`, bits [17:16] = 01) arrives.
  Forcing the bits on just turned a silent max-cycle hang into
  `timeout happen` on all four threads, hiding the missing wake-up.

  Isolated on 2026-09-24 in `/home/potato/openpiton-b` (upstream monitor):
  OpenPiton's exact CI recipe (`sims -sys=manycore -vlt_build/-vlt_run
  -x_tiles=1 -y_tiles=1 princeton-test-test.s`, full monitoring, no MACE
  flags) stalls the same way under Verilator 5.020 with `--no-timing`
  ("terminated by reaching max cycles"), so neither MACE's
  `MINIMAL_MONITORING` nor its cache flags cause it. Verilator 5 refuses
  this design without `--timing`/`--no-timing` (NEEDTIMINGOPT in
  `sas_intf.v`/`sas_task.v`), and `--timing` aborts at startup ("Missed a
  time slot?") because `piton/tools/verilator/my_top.cpp` advances time by
  hand. CI uses Verilator 4, which ignores delays too, so the likeliest
  cause is Verilator 5's rewritten scheduler exposing an ordering race
  somewhere on the interrupt path; confirming that needs a Verilator 4
  build (the local `/home/potato/verilator` clone has the 4.x tags, but one
  of its pack indexes is corrupt).

One-shot's two distinct failure modes are themselves informative. For sparc
it produced a config that looked plausible and got as far as a real build,
but the hardware run still failed silently, with no verdict at all --
exactly the class of failure an ungated single guess can't catch, because
nothing downstream of it ever checks. For pico it never even reached
hardware: the LLM proposed `l15_size=0`, and `PitonConfig`'s own
construction-time validation (`chia_openpiton/state_def.py`, "cache ...
size/associativity must be positive") correctly rejected it --
`examples/baseline_one_shot_llm.py` has no retry path for a malformed LLM
response at all, so this is simply where that baseline stops.

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

## 9. Setting up your own machine — especially the clustering side

This section is for you specifically if your main work is the cluster/GCP
side. Nothing about this project is Windows/WSL-specific — that's just the
host this was developed on — everything below works the same on native
Linux, only without the WSL layer.

### 9.1 The local half (same on any machine)

```bash
conda create -n chia_env -c conda-forge --override-channels python=3.10.19
conda activate chia_env

git clone https://github.com/ucb-bar/chia.git
pip install -e ./chia

git clone https://github.com/ranaumarnadeem/MACE.git
cd MACE
pip install -e ".[test]"
pytest chia_openpiton/test mace/test -q --ignore=chia_openpiton/test/cluster --ignore=mace/test/cluster
```

That last command should be green with no real hardware or cloud access at
all — it's the right first checkpoint before touching GCP.

For real builds (not just tier-0 tests), you also need a real OpenPiton
checkout, patched once:

```bash
git clone https://github.com/PrincetonUniversity/openpiton.git ~/openpiton
cd ~/openpiton && git checkout 1c6bfd2 \
    && git submodule update --init --recursive piton/design/chip/tile/ariane
bash /path/to/MACE/scripts/patch_openpiton.sh ~/openpiton
```

Build the checkout on **native Linux storage**, not a Windows-mounted path —
`chia_openpiton/README.md` measured a 1×1 Ariane build at 37s on ext4 versus
several minutes on `/mnt/c`, and (Section 7 above) a Windows-mounted checkout is
where the symlink and CRLF bugs came from in the first place. If you're on
WSL, clone into your Linux home directory (`~/openpiton`), not
`/mnt/c/...`.

You'll also need Verilator and (for Ariane) a `riscv64-unknown-elf` GCC
covering `rv64imafdc`/`lp64d` on `PATH` — `apt install verilator` plus the
prebuilt toolchain URL in `cluster/local.yaml`'s own `setup_commands` (search
for `riscv-gnu-toolchain`) is exactly what the GCP worker setup below
installs, and is the fastest way to get an identical local setup.

### 9.2 Getting onto the same GCP project

The project credits live under GCP project **`mace-508004`**. To actually
share the same project (same quota, same billing, the exact setup that's
already been proven to work) rather than standing up your own from scratch,
two things need to happen — one on the project owner's side, one on yours.

**The project owner grants you access, once:**

```bash
gcloud projects add-iam-policy-binding mace-508004 \
    --member="user:her-email@example.com" \
    --role="roles/compute.admin"
```

(Substitute the real email. `roles/compute.admin` is enough to create,
list, and tear down GCE instances — if something's still denied, the
fallback is `roles/editor`, broader but simpler.) This is a GCP Console/CLI
action only the project owner can do — I can't grant this myself.

**You then authenticate as yourself, once:**

```bash
gcloud auth login                                          # your own Google account
gcloud config set project mace-508004
gcloud auth application-default login
gcloud auth application-default set-quota-project mace-508004
pip install google-cloud-compute
```

Confirm the Compute Engine API is enabled on the project (`gcloud services
list --enabled | grep compute` — ask the owner to enable it if it's missing,
that's also a project-level action).

**What does *not* need to be shared:** your SSH key (`GCP_SSH_KEY` — generate
your own, e.g. `ssh-keygen -t ed25519`) and your Tailscale account/auth key
(`TS_AUTHKEY` — sign up for your own free Tailscale account and generate a
reusable auth key at `login.tailscale.com/admin/settings/keys`). These are
per-machine: `cluster/local.yaml`'s `tailnet:` block has each `chia up`
session build its own private tailnet between your own head and your own
GCP worker — you don't need to be on the *same* tailnet as the project
owner for your own cluster to work end-to-end, only the same GCP project
for the compute quota/billing to line up.

### 9.3 Bringing the cluster up

```bash
export HEAD_IP=$(hostname -I | awk '{print $1}')   # your machine's IP, for CHIA to SSH into
export GCP_PROJECT=mace-508004
export GCP_SSH_KEY=$HOME/.ssh/id_ed25519             # your own key from 9.2
export TS_AUTHKEY=<your own reusable Tailscale auth key>

chia up cluster/local.yaml
# ray status  -- or ray.nodes() from Python -- should show your local node
# and the GCP worker, both Alive, advertising {"openpiton": 2} and
# {"openpiton": 8} respectively.
```

Read `cluster/local.yaml`'s own header comments before your first run — it
documents the exact machine type, zone, and disk size this project already
validated (`e2-standard-8`, `us-central1-b`, 64GB, on-demand not spot — the
comments explain the real stockout/quota issues that produced these exact
choices, worth knowing before you change any of them).

**When you're done, always:**

```bash
chia down cluster/local.yaml
gcloud compute instances list      # confirm zero running instances
```

Real, billed compute — check the instance list after every session, not just
when something looks wrong. This project's own standing rule throughout: never
assume a teardown succeeded, verify it.

## 10. Run this yourself: a real Ariane walkthrough

The safest, fastest thing to actually run and see pass — no GCP needed, just
the local setup from Section 9.1.

**Step 1 — drive the adapter directly** (a few minutes, mostly the build):

```python
from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

node = OpenPitonWorkspaceNode("/home/you/openpiton")
cfg = get(node.configure.chia_remote(x_tiles=1, y_tiles=1, core="ariane"))
print(cfg.build_id)                                    # e.g. mace_a1b2c3d4e5f6

art = get(node.build.chia_remote(cfg))
print(art.success, art.wall_time_s)                     # True, ~37-100s depending on storage

res = get(node.run.chia_remote(cfg, "hello_world.c"))
print(res.verdict)                                       # "pass"
node.close()
```

What you should see: `art.success` is `True`, and `res.verdict` is the
literal string `"pass"` — read from `sim.log`'s own
`Simulation -> PASS (HIT GOOD TRAP)` line, not from a process exit code
(Section 2 explains why that distinction matters). If `res.verdict` is anything
else, check `res.sim_log_tail` first — it's the actual simulator transcript,
and will tell you far more than a stack trace would.

**Step 2 — run the full agentic loop** (30-40 minutes; it makes real LLM
calls and real hardware builds, and currently prints nothing until the whole
run finishes — see Section 12 below for why that's exactly the kind of thing a
real CLI should fix):

```bash
conda activate chia_env
python examples/mace_end_to_end.py \
    --piton-root /home/you/openpiton \
    --model opencode/big-pickle \
    --max-iterations 3
```

You'll get a per-iteration breakdown and the five summary metrics
(successful tasks, iterations, failures recovered, execution time, compute
cost) at the end, and everything lands in `runs/mace_end_to_end.db` — you can
query it directly:

```python
import sqlite3
con = sqlite3.connect("runs/mace_end_to_end.db")
for row in con.execute("SELECT run_id, status FROM runs"):
    print(row)
```

**Step 3 — try the cluster version once Section 9 is set up:** the same
`mace_end_to_end.py` command works unchanged once `chia up cluster/local.yaml`
is running — `ray.init()` inside the script picks up whatever cluster is
already live. Pass `--piton-root-2` pointing at a second checkout to see real
parallel fan-out across two machines.

## 11. Open problems — genuinely scoped things you could pick up

Ordered roughly by value-per-effort, not urgency — none of these block the
paper or the core submission, which stand on their own already. A few items
from the last pass have since closed out; kept here with their outcome
noted rather than silently deleted, so you can see what actually happened.

1. **[ucb-bar/chia#72](https://github.com/ucb-bar/chia/issues/72) is resolved — read this before
   assuming the old diagnosis still holds.** What looked like a real CHIA-side scheduler bug
   (task leases from the GCS never reaching a tailnet-relayed worker's raylet, confirmed via a
   raylet state-dump during a patient six-minute retest) turned out to be a launch-configuration
   gap on our own side: CHIA sets three proxy env vars (`RAY_grpc_enable_http_proxy`,
   `grpc_proxy`, `no_grpc_proxy`) on each node during `chia up`, inherited automatically by
   `chia job submit` but not by a driver launched manually via `python driver.py` — exactly what
   every script in this repo does — unless the launching shell sets the same three vars itself.
   A CHIA maintainer identified this; we verified it directly by reading the exact values off the
   head's own live raylet process (`/proc/<pid>/environ`) and exporting them before launching —
   the identical forced-placement task went from a six-minute hang to a two-second real execution.
   **This is now the correct pattern for any manually-launched driver against a tailnet cluster** —
   worth adding to `OpenPitonWorkspaceNode`/the cluster docs so nobody has to rediscover it.
   With dispatch fixed, the actual 2×2 build still hasn't reached a verdict — it hit a worker
   OOM/crash mid-build, the same memory-pressure class as the 4×4 finding below, now also seen on
   GCP. That's the real remaining work on this front, not the dispatch question.
2. **A real 4×4 fix** — Section 7 above has the exact diagnosis; forcing a real
   `-j1` into Verilator's own generated build-step `make` invocation (not
   just the environment) would likely get an actual 4×4 pass/fail verdict,
   strengthening baseline (a). Not yet attempted.
3. ~~Waveform-level tracing of the 2×2/pico hangs~~ — **pico half done**:
   waveform tracing found and fixed three real bugs (picorv32's own
   `resetn`/`booted` self-boot gate, the manycore monitor's `active_thread`
   tracking for pico, a real-silicon BIST self-clear race); pico now
   genuinely passes. **2×2 Ariane is still genuinely open** and is the
   deeper remaining work here. Start from the exact divergence point
   described in Section 7: boot/reset/IOB completes identically to a passing run,
   then the core never traps.
4. ~~`examples/mace_end_to_end.py`'s `--core` choices don't include `"pico"`~~
   — **done**: `--core=pico` and a `--workload` flag are both now exposed
   (the gate workload used to be silently hardcoded to `barrier_atomic.c`
   regardless of `--objective` text).
5. **A real hardware proof of the fail→triage→replan→pass cycle** (Section 7's
   "one honest gap") — still open, now checked four separate ways across
   seven real runs, worth reading before trying a fifth. Attempts so far,
   weakest to strongest: (a) an unfamiliar workload (`producer_consumer.c`,
   then `scatter_gather.c`) on the theory the LLM has no prior converged-on
   answer for it — passed cleanly both times; (b) an LLM-chosen "reduced"
   L1D cache — still passed; (c) a **mandatory, explicit** extreme L1D
   geometry (128 bytes, direct-mapped) that both the human prompt and the
   planning agent's own task description explicitly predicted would fail —
   verified via the real `sims` invocation (not the agent's self-report) to
   have actually been used — and it passed anyway. The `failures` table in
   `runs/mace_end_to_end.db` was confirmed completely empty across all seven
   real runs as of when this was written -- **stale as of 2026-09-20**: the
   db is a live, appended-to local file, now at 14 runs with 3 failure rows
   (all a `budget_exceeded` run's real toolchain/environment failures, not a
   hardware RTL one -- see Section 7's own update note for the current numbers).
   Our read on the original seven: this isn't a broken test design, it's a
   genuine finding that this RTL's coherence protocol doesn't functionally
   depend on L1D capacity for these access patterns, at least down to one
   cache line.
   If you want to close this gap, going more extreme than a 1-line
   direct-mapped cache risks testing "is a malformed parameter rejected"
   rather than genuine coherence robustness — a fundamentally different,
   less interesting question. A more promising lever untried so far: a mesh
   shape or NoC parameter, rather than cache geometry. See Section 10's walkthrough
   for how to kick one off, and `scripts/ariane_1x1_tiny_l1d_scatter_gather.py`
   for the exact script that produced finding (c).
6. ~~The mystery file, `examples/run_barrier_atomic.py`~~ — **done**:
   committed, it's a real, useful manual driver for baseline (a).
7. ~~**Paper polish**~~ — **done**: the architecture figure (Figure 1) and
   the author byline (Rana Umar Nadeem, Samrah Mumtaz, and Muhammad Imran)
   are both in `paper/mace_paper.tex`.
8. **`docs/api/openpiton.rst`** — only matters if actually filing a PR to
   upstream CHIA; the checklist for that is in
   [`chia_openpiton/README.md`](../chia_openpiton/README.md)'s last section.
9. **The CLI's own gaps** — Section 12 has the details: ~~`mace cluster up/down/status`
   isn't wired in yet~~ and ~~no non-interactive script-file mode~~ — both
   **done**: a thin subprocess wrapper over `chia up`/`chia down`/`ray status`
   (Section 9's manual sequence still works too, this is just the same commands
   behind the one CLI), and `shell --script`/`-c` (Yosys's own `-c script.ys`
   convention). Still open: `run`/`init` only have tier-0 test coverage so
   far, not a real end-to-end pass through `MaceShell` itself.

## 12. The `mace` CLI

A real `mace` command exists now (`mace/cli/`, entry point in
`pyproject.toml`'s `[project.scripts]`) — `pip install -e .` puts it on
`PATH`. Deliberately modeled on Yosys/OpenROAD's own interactive-shell
convention rather than a set of independent subcommands: `read_verilog`,
`top_module`, `read_spec`, and `set_core` each accumulate state in one
session, and `run` acts on everything gathered so far.

```
mace init --backend opencode --api-key <key>              # once: writes ~/.mace/.env + a doctor-style env check
mace shell --piton-root /path/to/openpiton --api ~/.mace/.env --backend opencode   # starts the interactive session

mace> read_verilog my_core.v my_core_pkg.v
mace> top_module my_core_top
mace> read_spec objective.txt
mace> set_core 4
mace> run
mace> write_report > result.rpt
mace> exit
```

`init` combines what a separate `doctor` command would have done with
credential setup, per the project owner's own instruction — one command,
not two. It checks for `verilator`, `riscv64-unknown-elf-gcc`, and `git` on
`PATH`, confirms `ray`/`chia_openpiton` import, and writes the right
backend-specific environment variable to a plain `.env` file (default
`~/.mace/.env`, owner-only permissions — see `mace/cli/config.py`'s own
docstring). `shell` requires `--api <path>` pointing at that file on every
launch — deliberately not auto-loaded from a fixed, invisible location: a
live user hit a stale saved key with no easy way to see what was actually
in play, and a path you name yourself is one `cat` away from being
debuggable. `shell` fails fast, before touching Ray or the LLM backend, if
the file is missing or doesn't set the variable `--backend` expects.

**`--backend vertex` is the one exception to `--api` being mandatory**, and
it's a real, confirmed-working path, not just plumbing: Google's Vertex
Gemini backend (`chia.models.vertex.VertexGeminiLLM`) authenticates via
Application Default Credentials, not a literal key string — `gcloud auth
application-default login` (already set up on this project's own dev
machine from the earlier GCP cluster work) is enough, so `--api` can be
omitted entirely for it. `--model` becomes required instead (Vertex has no
usable default — `VertexGeminiLLM` raises `TypeError` with none set).
Confirmed against the real `mace-508004` project: `gemini-2.0-flash-001`
404s (not available on this project/region), `gemini-2.5-flash` works. A
full real loop run (`mace shell --backend vertex --model gemini-2.5-flash`,
1x1 Ariane, `barrier_atomic.c`) completed end to end and passed —
`chia_openpiton`'s own MCP tool-calling loop working correctly against
Gemini, not just OpenCode. Worth noting since it was a live point of
confusion: this project has used GCP heavily throughout, but always as
*compute* (the cluster workers actually building/running RTL) — the LLM
backend that plans/diagnoses has been OpenCode by default the whole time
(see `mace/llm.py`'s own docstring); Vertex-as-LLM-backend and
GCP-as-compute are two independent things that happen to share a cloud
provider.

**`read_verilog`/`top_module` and the honest "why not" answer.** This is
where the CLI directly answers the "can we pass it any core" question from
earlier in this project: `top_module`'s declared name is matched (case-
insensitive substring) against the only cores chia_openpiton actually has an
L15 adapter for — `ariane`, `sparc`, `pico` (see `mace/cli/session.py`'s
`detect_core`). If it matches, `run` drives the real MACE loop against that
core. If it doesn't, `run` does **not** attempt a fake integration or spend
30 minutes of real hardware time to eventually shrug — it immediately
produces the same structured `PostMortem` Section 11 item 5 already builds
(`assessment=likely_hardware_limitation`), explaining precisely why (no
generic core-to-NoC bridge exists; every core needs hand-written coherence-
adapter RTL) and what adding real support would actually take (see
`mace/cli/shell.py`'s `no_adapter_post_mortem`). This is a static, structural
answer, not an LLM guess — it's already a known fact about the project's own
real boundary, the same way `mace doctor`-style checks are static facts
about the environment.

**`set_core <N>`** picks a mesh shape for *N* total tiles (square when *N*
is a perfect square, matching every mesh this project has ever actually
built) and immediately reports what's actually known about that shape —
`KNOWN_MESH_OUTCOMES` in `mace/cli/session.py` encodes the real, hard-won
findings from Section 7: 1 tile is validated repeatedly, and 4 and 16 tiles
(2x2, 4x4) pass for ariane (`barrier_atomic.c`) and pico (`addi.S`), with
every tile reaching `Hit Good trap`. Anything else is accepted but flagged
as genuinely unvalidated, not silently treated the same as a known-good
shape.

**Logging is verbose by default**, per the project owner's own instruction,
not an opt-in flag (`-verbose`/`--verbose` are accepted for EDA-tool
familiarity but change nothing). `run_mace_loop` gained an `on_iteration`
callback (`mace/orchestrator.py`) specifically so the CLI can print real
incremental progress instead of nothing until the whole run finishes: each
config task prints as "adding" the cache/config change with its build
status, each workload task prints as "running verification" followed by the
*actual* verification log tail (`sim.log`, `status.log`) — not just a
pass/fail summary.

**`write_report > <name>.rpt`** writes the last run's outcome (metrics
summary plus the post-mortem, if one exists — including the static
no-adapter case) to a file, in the same spirit as a real EDA tool's `.rpt`
convention.

**`run -coverage`** is real, unlike `-verbose` — it sets `Session.coverage`
(sticks across future `run`s in the same session, matching `set_core`'s own
accumulates-until-changed convention) and threads `MaceSpec.coverage` into
every `PitonConfig` the loop builds (`mace/loop.py`, `mace/integrator.py`),
appending `chia_openpiton.state_def.COVERAGE_LINE_FLAG` to `extra_flags`.
Once a run passes, the shell locates the last task's `coverage.dat`
(`find_coverage_dat`) and runs `verilator_coverage --annotate` on it
(`generate_coverage_report`) — via `resolve_verilator_coverage()`, which
prefers whatever's first on PATH (keeping annotation self-consistent with
whatever actually built the model, since coverage.dat's format is tied to
the Verilator version that wrote it) and falls back to the *system*
binary's absolute path (`/usr/bin/verilator_coverage`) when nothing on
PATH resolves or what's there faults on `--version`: a bare conda-env
shell's own install can be genuinely broken this way, confirmed directly,
unrelated to any specific coverage.dat. Needs a real patch to build on top
of, too — OpenPiton's own hand-written testbench
(`piton/tools/verilator/my_top.cpp`) never called Verilator's
coverage-write API, so `scripts/patch_openpiton.sh` fix 5 adds that call,
guarded by `#if VM_COVERAGE` (Verilator's generated Makefile always
defines this macro to 0 or 1, never leaves it undefined — an earlier
`#ifdef` form of this guard was always true regardless of value, a real
bug this project hit and fixed: every plain, non-coverage build failed to
link with "undefined reference to VerilatedCov::..."). Real result, not a
mock: 36.00% (8787/24311) on a real passing 1x1 Ariane +
`barrier_atomic.c` run, and 35.00% (9497/26900) on a passing 2x2 run
(2026-09-24, build `mace_3b2325c22a9b`, 235s build, all four tiles hit their
good trap; the paper's figure). See `scripts/local_coverage_1x1_build_test.py`'s own
module docstring for the full account, including why the plan's original
`--report hier` idea doesn't work on this machine (that flag doesn't exist
on the *stable* Verilator's `verilator_coverage`, only the broken one's).

### What's deliberately not built yet

- **`chia job submit`-based dispatch** — the shell currently launches
  `ray.init(address="local", ...)` directly, the same pattern every other
  local script in this repo uses; it doesn't yet drive a real `chia up`
  cluster the way `examples/mace_end_to_end.py` can be pointed at one
  manually.

### Testing

Tier-0 only so far (`mace/test/test_cli.py`, 47 tests) — every piece of real
logic (`detect_core`, `mesh_for_core_count`, `parse_spec_file`, each
`handle_*` command function, `no_adapter_post_mortem`, `format_report`,
config save/load) is a free function independent of `cmd.Cmd`'s own input
loop, called directly in tests rather than driven through simulated
keystrokes — the same split `mace.triage` keeps between `build_prompt`
(pure) and `triage` (does the LLM call). `run_mace_loop`'s new
`on_iteration` callback has its own tier-1 coverage in
`orchestrator_e2e_test.py::TestOnIterationCallback`. Not yet covered: the
shell's actual `run` command end-to-end (needs a real or stubbed Ray
dispatch through `MaceShell` itself, not just its handler functions) and the
`init` command's doctor checks against a real missing-toolchain case.

### Suggested build order — superseded

This originally planned a `mace doctor` / `configure` / `build` / `run` /
`loop` sequence of standalone subcommands, in that order. What actually got
built took a different, now-complete shape instead: one `mace init` (folding
in doctor-style checks, see above) plus one `mace shell` REPL whose
`read_verilog`/`top_module`/`set_core`/`run`/`write_report` commands
accumulate session state and drive the real loop, including real
`on_iteration` progress (see above) — not a set of independent subcommands.
`mace results` (a read-only cross-run table plus a per-run
`--run-id`/failure-taxonomy view, over `mace.metrics.all_runs`/
`failure_taxonomy`), `mace cluster up/down/status` (a thin subprocess
wrapper over `chia up`/`chia down`/`ray status`), and `shell --script`/`-c`
(a non-interactive Yosys-`-c`-style mode) have since been built too. The
remaining real gap is `chia job submit`-based dispatch (see "What's
deliberately not built yet" above).

## 13. Where to find more

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
- `cluster/local.yaml` — read its header comments directly before your first
  `chia up`; they carry the exact machine type/zone/quota decisions and why,
  in more operational detail than Section 9 above repeats.
