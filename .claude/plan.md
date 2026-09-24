# How MACE was planned and built

MACE was built for the A3 CHIA Hackathon at MICRO 2026, between September 3 and 24, 2026.
This file records how the authors planned and built it with an AI coding assistant, and what comes next.
The rules that came out of this work are in `CLAUDE.md` and `claude_docs.md`.

## Two uses of AI

MACE calls an LLM itself: its planner, task agents, and failure analysis use Gemini 2.5 Flash on Vertex AI.
That is the system under study, and the paper measures it.

Separately, the authors built MACE with Claude Code, Anthropic's coding assistant.
The rest of this file is about that second use.

## How the work ran

- The project started from the authors' proposal and design document.
- Claude Code turned the design into a dated implementation plan for the build window of September 5 to 20, kept in one file and extended as work landed.
  Each phase listed its steps, the checks that would show it worked, and its risks.
  After each phase, a new section recorded where the work had departed from the plan and why.
- Work on a phase began once the authors approved its plan.
- Code changes landed in small commits with their tests, written first against a captured log or a stub checkout.
- Claims were checked by running something: tier-0 tests for logic, tier-1 tests on a local Ray instance, and Verilator simulation for anything about the hardware.
- At times, several Claude Code sessions ran at once on separate git worktrees, for example one fixing bugs while another drafted docs. Their commits were reviewed before merging.
- Claude Code review passes over the codebase produced numbered findings, and each confirmed finding was fixed in its own commit.
- The authors set the scope: which cores and meshes to evaluate and which results to report.
- Text posted outside the repository, such as the CHIA issue ucb-bar/chia#72, was reviewed by the authors before posting.
- The paper was drafted and revised with Claude Code. The authors edited it in Overleaf and drew Figure 1 themselves.
- The docs pages were drafted with Claude Code from the code. One author added them through pull requests, and review edits went into the same pull requests before merging.
- The rules in `CLAUDE.md` and `claude_docs.md` grew out of the authors' corrections. When an output missed, such as a banned word, an unchecked claim, or a commit message that ran long, the correction became a rule and applied from then on.

## Phases

### Phase 1: the `chia_openpiton` adapter (September 5 to 7)

Planned: a CHIA adapter for OpenPiton modeled on CHIA's ESP adapter, with configure, build, run, and collect operations, parsers tested against captured simulator logs, and five acceptance tests.

What changed:

- The plan pinned Verilator 4.014, which would not build on the host. The adapter reads the worker's Verilator version instead and adds `--no-timing` for version 5.
- A Docker worker image was built and then dropped. Workers are set up with plain setup commands.
- A successful build leaves a marker, and a later build of the same configuration skips `sims`.
- Newer binutils and GCC releases broke OpenPiton's boot ROM. The fixes became the first two entries in `scripts/patch_openpiton.sh`.

Checked: tier-0 tests on captured logs, an Ariane build that passed `hello_world.c` in Verilator, and parallel builds across two checkouts. The GCP acceptance test moved to later.

### Phase 2: the MACE loop (September 7 to 8)

Planned: eleven steps, each testable alone, with the wiring built before the planner: the run spec, a fake LLM, plan-line parsers, a one-task loop, the integrator, gate workloads, parallel fan-out, metrics, the planner, the LLM backend switch, and replay.

What changed:

- The gate workloads read shared data through atomic operations, since a plain load of shared memory was not reliably visible in Verilator simulation of this RTL.
- Every run passes an explicit RTL timeout. The first fan-out test on Ariane timed out without one.
- Replay re-serves the return values of tagged calls. CHIA's cache sees only return values, so replay does not reproduce the file edits an agent makes.

Checked: each step's tier-0 tests, then tier-1 runs on local Ray and full runs on Ariane.

### GCP and the first multi-tile builds (September 8 to 16)

- A GCP worker joined the cluster over Tailscale, but tasks never started on it. The investigation went to CHIA as ucb-bar/chia#72. Exporting the proxy variables that `chia up` sets fixed the first hang; dispatch to a tailnet-relayed worker is still unreliable.
- Multi-tile work moved to a local checkout. Symlinks stored as text and CRLF line endings from a Windows checkout became patch fixes 3 and 4. The 2x2 Ariane run then hung, and 4x4 builds ran out of memory, which led to `MAKEFLAGS=-j1` and patch fix 9.

### A third core: PicoRV32 (September 9 and 19)

- Planned: select PicoRV32, whose OpenPiton adapter already existed upstream, and get a pass with `addi.S`. OpenPiton's CI builds this core but never runs it.
- September 9: the build passed, and the run hit the cycle limit without reaching a trap. The compiled binary was correct, and the boot log matched a passing Ariane log up to the point where the core should start, which ruled out the toolchain and the build. The work paused there to leave time for the baselines and the paper.
- September 19: waveform tracing found three causes. The core waited for a wake-up interrupt nothing sends (fix 6), the monitor never marked its thread active (fix 7), and the SRAM self-test clear discarded its first memory writes (the `CONFIG_DISABLE_BIST_CLEAR` define). PicoRV32 then passed.

### Tooling (September 15 to 21)

- The `mace` CLI, an interactive shell in the style of Yosys and OpenROAD.
- Verilator line coverage from build to report (patch fix 5).
- A Nix development shell that pins Verilator 5.052 and the RISC-V toolchain.
- The `unit_test` task kind: MACE scaffolds a unit-test environment for one RTL module and gives the agent an edit tool limited to that testbench and the module's source.
- Failure-analysis tools that compare a failed transcript with a passing one and show the compiled program's symbols beside the run's symbol table.
- Vertex AI with Gemini 2.5 Flash as the default for the CLI and the example drivers.

### Multi-tile passes and the evaluation (September 18 to 24)

- The planner gained `CACHES:` and `CONFIG_RTL:` lines for per-task cache geometry and RTL defines, and the driver gained `--mesh`.
- Under Verilator 5, CVA6's L1.5 adapter drops cache invalidations (verilator#5829), so the exit barrier in `syscalls.c`, which polled with plain loads, never saw other tiles' updates. Fix 11 polls with atomic reads.
- The monitor's 32-bit finish mask covered only 8 tiles, so a 4x4 run could pass with half its tiles unfinished. Fix 10 widens it.
- CVA6's instruction tracer wrote every hart to one file. Fix 12 names the file per hart, so each tile's trace can be checked.
- The loop then passed 2x2 and 4x4 meshes of Ariane and PicoRV32.
- The evaluation compared manual bring-up, a one-shot LLM configuration, and the loop (Table 1 of the paper). The one-shot baseline got the same RTL-define field as the loop, so both could request the define PicoRV32 needs.
- The paper moved to IEEE format within 4 pages, and the docs became a Sphinx site in the CVA6 layout.

## Next

- Add two to four more RISC-V cores. All three current cores already had an upstream adapter to OpenPiton's L1.5 cache. A core without one, such as Ibex or Rocket Chip, needs that adapter written first.
- Make adding a core a repeatable process: a template built from the shape the three adapters share (a per-core RTL directory, a transducer from the core's memory interface to the L1.5, and a core-select arm in the tile template), with the loop verifying each revision of the adapter RTL in Verilator as an engineer writes it.
- Cores with their own coherent cache, like Rocket Chip, need new coherence RTL. Cores without one, like Ibex, need a smaller transducer.
- Prove the recovery cycle on live failures. So far a stub that fails once exercises detect, diagnose, and replan.
- Add a dashboard over the run database that shows dispatch, build, and verification as they happen.
- Open issues: OpenSPARC T1 stalls under Verilator 5.020, and dispatch to a tailnet-relayed GCP worker is unreliable.
