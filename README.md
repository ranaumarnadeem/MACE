# MACE: Multicore Agentic Co-Design Engine

**MACE is an LLM-agent system that turns a single-core design into a verified multicore OpenPiton system: it plans, builds and simulates OpenPiton meshes on the CHIA framework from a hardware objective written in English.**

You give MACE an objective, a core (Ariane/CVA6 or PicoRV32) and a target mesh size. An LLM planner turns the objective into a task DAG; independent tasks run in parallel, one per OpenPiton checkout. A configuration passes only when its Verilator build succeeds and every tile of the simulated mesh reaches its good trap. When a task fails, a failure-analysis agent reads the logs and the planner replans, until the design passes or the run's budget is spent. The agents do not edit the cores; MACE reuses OpenPiton's NoC, caches and coherence infrastructure to make the system multicore.

- Accepted as a poster at the A3 workshop, MICRO 2026
- Built for the A3 CHIA Hackathon at MICRO 2026
- Documentation: https://ranaumarnadeem.github.io/MACE/
- Paper: [`paper/mace_paper.pdf`](paper/mace_paper.pdf)
- Proposal: [`CHIA_proposal.pdf`](CHIA_proposal.pdf)

## Two parts

- `chia_openpiton/` is a CHIA adapter for OpenPiton. It configures, builds,
  runs, and collects results through OpenPiton's `sims` tool. It imports nothing
  from `mace`, so it can move into CHIA as `chia/openpiton/`.
- `mace/` is the agentic loop on top of the adapter: planner, parallel task
  execution, verification gate, failure analysis, budgets, replay tags, and a
  SQLite run database, plus the `mace` command-line tool.

## Results

The MACE loop brought all four configurations (Ariane and PicoRV32, 2x2 and 4x4)
to a passing Verilator simulation; the one-shot LLM configuration failed all
four. Ariane (CVA6) runs `barrier_atomic.c` and PicoRV32 runs `addi.S`. Table 1
of the paper compares the loop with manual bring-up and the one-shot
configuration:

| Core | Mesh | Manual | One-shot LLM | MACE loop |
|---|---|---|---|---|
| Ariane | 2x2 | pass, 33.9 s | fail, 10.4 s \* | pass, 750.1 s |
| Ariane | 4x4 | pass, 271.9 s | fail, 3820.9 s | pass, 250.9 s |
| PicoRV32 | 2x2 | pass, 9.8 s | fail, 12.0 s \* | pass, 70.7 s |
| PicoRV32 | 4x4 | pass, 53.9 s | fail, 13.4 s \* | pass, 100.6 s |

\* Invalid configuration (L1.5 size 0), rejected before any build.

[Results](https://ranaumarnadeem.github.io/MACE/06_mace_evaluation/results.html)
explains each run. OpenSPARC T1 builds but does not run under the Verilator 5
used here.

### Run records

The logs and SQLite run databases behind every result, including the table
above, the seeded-failure runs on the Results page, and the coverage run, are in
[`mace-0.0.1-runs.zip`](https://github.com/ranaumarnadeem/MACE/releases/download/v0.0.1/mace-0.0.1-runs.zip),
attached to the
[0.0.1 pre-release](https://github.com/ranaumarnadeem/MACE/releases/tag/v0.0.1).
Its `runs/RUNS.md` names the log and run ID behind each result. Unzip it in the
repository root, then list the recorded runs, or show one run's plan, builds,
and failure analysis:

```bash
mace results --db-path runs/mace_end_to_end.db
mace results --db-path runs/mace_end_to_end.db --run-id fd0c370667aa --trace
```

## Install

MACE needs Python 3.10, CHIA, Verilator, a RISC-V GCC toolchain, and a patched
OpenPiton checkout. CHIA is installed from its v1.0.1 release; this repository
does not fork it.

With Nix (flakes enabled), one command sets up all of it except OpenPiton:
Python, Verilator 5.052, the RISC-V toolchain, OpenPiton's system packages, and
a `.venv` with CHIA and MACE installed. The first run downloads them and takes a
few minutes.

```bash
nix develop
pytest chia_openpiton/test mace/test --ignore=chia_openpiton/test/cluster --ignore=mace/test/cluster
```

Without Nix, use conda and install Verilator and the toolchain yourself;
[Installation](https://ranaumarnadeem.github.io/MACE/01_mace_user/Installation.html)
lists them:

```bash
conda create -n chia_env -c conda-forge --override-channels python=3.10.19
conda activate chia_env
pip install chialoops==1.0.1
pip install -e ".[test]"
pytest chia_openpiton/test mace/test --ignore=chia_openpiton/test/cluster --ignore=mace/test/cluster
```

These tests need neither Ray nor OpenPiton. To build and simulate, clone
OpenPiton on native Linux storage and patch it:

```bash
git clone https://github.com/PrincetonUniversity/openpiton.git ~/openpiton
git -C ~/openpiton checkout 1c6bfd2
git -C ~/openpiton submodule update --init --recursive piton/design/chip/tile/ariane
bash scripts/patch_openpiton.sh ~/openpiton
```

The patch script applies thirteen numbered fixes and is safe to run again.

## Run

The end-to-end driver runs the loop once and records it in
`runs/mace_end_to_end.db`. It uses Gemini 2.5 Flash on Vertex AI by default:

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=<your-gcp-project>
python examples/mace_end_to_end.py --piton-root ~/openpiton --core ariane --mesh 2x2
```

`--mesh` sets the size of every build. `--piton-root-2` adds a second checkout,
so independent tasks run in parallel. Before a 4x4 run, export `MAKEFLAGS=-j1`
to keep the Verilator C++ compile within memory.

The `mace` command runs the same loop from an interactive shell in the style of
Yosys and OpenROAD:

```text
mace shell --piton-root ~/openpiton --backend vertex
mace> read_spec objective.txt
mace> set_core 4
mace> run
mace> write_report > result.rpt
```

`mace results --db-path runs/mace_end_to_end.db` lists past runs, and adding
`--run-id <id> --trace` shows one run's plan, dispatch, and failure analysis.
[Quick Start](https://ranaumarnadeem.github.io/MACE/01_mace_user/Quick_Start.html),
[Interactive Shell](https://ranaumarnadeem.github.io/MACE/01_mace_user/Interactive_Shell.html),
and [Baselines](https://ranaumarnadeem.github.io/MACE/01_mace_user/Baselines.html)
cover the flags, the shell commands, and the comparison runs.

## Layout

| Path | Contents |
|---|---|
| `chia_openpiton/` | CHIA adapter for OpenPiton, with its own [README](chia_openpiton/README.md) |
| `mace/` | The loop and the `mace` command |
| `examples/` | End-to-end driver, baselines, and seeded-failure runs |
| `scripts/` | OpenPiton patch script and standalone build and run scripts |
| `docs/` | Source of the documentation site |
| `paper/` | The 4-page paper and its figures |
| `cluster/` | CHIA cluster config for `chia up` |
| `flake.nix` | Nix development shell |
| `dockerfiles/` | An earlier worker image, unused by the current setup |

## FAQ

**Can an LLM agent build a multicore RISC-V system?**
MACE does this for OpenPiton: from an English objective it configures, builds and verifies 2x2 and 4x4 meshes of Ariane or PicoRV32 cores, replanning on failure until simulation passes or the run's budget is spent.

**Does MACE change the CPU core?**
MACE's agents do not edit the cores. They configure OpenPiton's tiles, caches and NoC around them and can add RTL defines. Before the first build, `scripts/patch_openpiton.sh` fixes three bugs in OpenPiton's copies of the cores: PicoRV32 never booting from reset (fix 6), CVA6's tracer writing every hart to one file (fix 12), and CVA6's L1.5 adapter dropping invalidations under Verilator 5 (fix 13).

**Can I use the OpenPiton support without the agents?**
Yes. `chia_openpiton/` is a standalone CHIA adapter for OpenPiton with no dependency on `mace/`.

**What simulator does it use?**
Verilator, through OpenPiton's `sims` tool.

## Related projects

- [OpenPiton](https://github.com/PrincetonUniversity/openpiton): the manycore research framework MACE builds on
- [CHIA](https://github.com/ucb-bar/chia): the agentic hardware framework MACE extends
- [faultflow](https://github.com/ranaumarnadeem/faultflow), [autoMBIST](https://github.com/ranaumarnadeem/autoMBIST): open-source DFT tools by one of the authors

## Contributing

AI coding assistants follow the rules in [`.claude/`](.claude/): `CLAUDE.md`
for code and runs, and `claude_docs.md` for writing.
[`.claude/plan.md`](.claude/plan.md) describes how the project was planned and
built with AI assistance. When you open a pull request, attach the instruction
file your tool used, as [`.claude/README.md`](.claude/README.md) asks.

## License

MACE is released under the BSD 3-Clause License; see [`LICENSE`](LICENSE).
This is pre-release version 0.0.1.

## Authors

Rana Umar Nadeem, Samrah Mumtaz, and Muhammad Imran.
