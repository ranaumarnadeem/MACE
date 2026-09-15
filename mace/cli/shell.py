"""mace.cli.shell -- the interactive command shell: init, read_verilog,
top_module, read_spec, set_core, run, write_report.

Modeled deliberately on Yosys/OpenROAD's own interactive-shell convention
(state accumulates across commands typed one at a time; `run` acts on
everything accumulated so far) rather than mace's existing example scripts'
convention (one process, one fully-specified invocation, one exit). Command
*handling* logic lives in free functions below, each independently testable
without driving the shell's own input loop; the ``do_*`` methods on
:class:`MaceShell` are thin I/O wrappers around them, the same split
mace.triage keeps between ``build_prompt`` (pure) and ``triage`` (does the
LLM call).
"""

from __future__ import annotations

import cmd
import os
import shlex
import sys
from pathlib import Path

import ray
import typer

from chia.database.sqlite_node import SQLiteNode
from mace.cli.config import apply_config_to_environment, load_config, save_config
from mace.cli.session import KNOWN_MESH_OUTCOMES, Session
from mace.cli.spec_file import parse_spec_file
from mace.llm import make_llm
from mace.metrics import get_post_mortem, open_db, record_post_mortem, summary
from mace.orchestrator import run_mace_loop
from mace.spec import Budget, MaceSpec, PostMortem
from mace.workloads import RECOMMENDED_RTL_TIMEOUT

app = typer.Typer(help="MACE: point it at a core, tell it what to verify, and watch it work.")


# ---------------------------------------------------------------------------
# init / doctor -- combined per the project owner's own instruction: init
# does what a separate "doctor" command would have done, plus credential setup.
# ---------------------------------------------------------------------------


def run_doctor_checks() -> list[tuple[str, bool, str]]:
    """Read-only environment checks: ``(name, ok, detail)`` for each.

    Deliberately has no side effects and needs no piton_root -- it checks
    what's on this machine's PATH, not any specific checkout. A checkout's
    own toolchain patches (scripts/patch_openpiton.sh) are a `read_verilog`/
    `run`-time concern, not this one's.
    """
    import shutil

    checks = []
    for tool in ("verilator", "riscv64-unknown-elf-gcc", "git"):
        path = shutil.which(tool)
        checks.append((tool, path is not None, path or "not found on PATH"))
    try:
        import ray as _ray  # noqa: F401

        checks.append(("ray", True, "importable"))
    except ImportError as e:
        checks.append(("ray", False, str(e)))
    try:
        import chia_openpiton as _co  # noqa: F401

        checks.append(("chia_openpiton", True, "importable"))
    except ImportError as e:
        checks.append(("chia_openpiton", False, str(e)))
    return checks


@app.command()
def init(
    backend: str = typer.Option("opencode", help="LLM backend: opencode, claude, antigravity, vertex"),
    api_key: str = typer.Option(
        None, prompt=True, hide_input=True, help="API key/credential for the chosen backend"
    ),
) -> None:
    """Set up credentials and check the environment -- run this first."""
    path = save_config(backend, api_key)
    typer.echo(f"Saved credentials to {path} (owner-only permissions).")
    env_var = apply_config_to_environment({"backend": backend, "api_key": api_key})
    typer.echo(f"Set {env_var} for this session (and every session after `init`).")

    typer.echo("\nEnvironment checks:")
    all_ok = True
    for name, ok, detail in run_doctor_checks():
        mark = "OK  " if ok else "MISSING"
        typer.echo(f"  [{mark}] {name}: {detail}")
        all_ok = all_ok and ok
    if all_ok:
        typer.echo(
            "\nEverything looks ready. Run `mace shell --piton-root /path/to/openpiton` "
            "to start the shell."
        )
    else:
        typer.echo(
            "\nSome tools are missing -- `read_verilog`/`run` may fail until "
            "they're on PATH. See docs/TECHNICAL_GUIDE.md section 9 for setup."
        )


# ---------------------------------------------------------------------------
# Command handlers -- pure(ish) logic, no cmd.Cmd/I/O dependency.
# ---------------------------------------------------------------------------


def handle_read_verilog(session: Session, arg: str) -> str:
    """`read_verilog <file> [file2 ...]` -- register RTL source files."""
    if not arg.strip():
        return "ERROR: read_verilog needs at least one file"
    files = [Path(f) for f in shlex.split(arg)]
    missing = [f for f in files if not f.exists()]
    if missing:
        return "ERROR: file(s) not found: " + ", ".join(str(f) for f in missing)
    session.verilog_files = tuple(files)
    return f"read {len(files)} file(s): " + ", ".join(f.name for f in files)


def handle_top_module(session: Session, arg: str) -> str:
    """`top_module <name>` -- declare the design's top-level module."""
    name = arg.strip()
    if not name:
        return "ERROR: top_module needs a module name"
    session.top_module = name
    core = session.detected_core
    if core is not None:
        return f"top module set to {name!r} -- matches supported core {core!r}"
    return (
        f"top module set to {name!r} -- does not match any core chia_openpiton "
        f"has an adapter for (ariane, sparc, pico). `run` will explain why "
        f"rather than attempt a fake integration."
    )


def handle_read_spec(session: Session, arg: str) -> str:
    """`read_spec <file>` -- parse a plain-text spec file into the session."""
    path = Path(arg.strip())
    if not arg.strip():
        return "ERROR: read_spec needs a file path"
    if not path.exists():
        return f"ERROR: file not found: {path}"
    overrides = parse_spec_file(path.read_text())
    if "objective" in overrides:
        session.objective = overrides["objective"]
    if "workloads" in overrides:
        session.workloads = overrides["workloads"]
    if not overrides:
        return f"read {path} but found no usable content (empty file?)"
    return f"read spec from {path}: " + ", ".join(f"{k}={v!r}" for k, v in overrides.items())


def handle_set_core(session: Session, arg: str) -> str:
    """`set_core <N>` -- set the target total tile count."""
    try:
        n = int(arg.strip().split()[0])
    except (ValueError, IndexError):
        return f"ERROR: set_core needs an integer tile count, got {arg!r}"
    try:
        mesh = None
        session.core_count = n
        mesh = session.target_mesh
    except ValueError as e:
        session.core_count = None
        return f"ERROR: {e}"
    known = KNOWN_MESH_OUTCOMES.get(n)
    line = f"target mesh set to {mesh[0]}x{mesh[1]} ({n} tiles)"
    if known:
        return f"{line} -- {known}"
    return f"{line} -- unvalidated in this project; chia_openpiton will attempt it, but no prior evidence either way"


def build_spec_from_session(session: Session) -> MaceSpec:
    """The session's accumulated state, turned into a real MaceSpec.

    Defaults match mace_end_to_end.py's own conventions: barrier_atomic.c
    if no workload was set, ariane if no core was ever declared (a plain
    read_spec/set_core/run session with no read_verilog at all is a normal,
    fully-supported MACE run, not an error).
    """
    core = session.detected_core or "ariane"
    return MaceSpec(
        workloads=session.workloads or ("barrier_atomic.c",),
        objective=session.objective or "Verify the gate workload passes.",
        core=core,
        target_mesh=session.target_mesh or (1, 1),
        budget=Budget(),
    )


def no_adapter_post_mortem(session: Session) -> PostMortem:
    """The immediate, static answer for a top_module that names no core
    chia_openpiton has an adapter for -- no LLM call, no hardware, because
    this is already a known fact about the project's own real boundary, not
    something worth spending 30 minutes of real compute to rediscover.
    """
    return PostMortem(
        assessment="likely_hardware_limitation",
        explanation=(
            f"'{session.top_module}' does not match any of the cores chia_openpiton has a "
            f"working L15 adapter for (ariane, sparc, pico). OpenPiton has no generic "
            f"core-to-NoC bridge -- every core needs a hand-written adapter translating its "
            f"own memory/cache-miss interface into OpenPiton's L15 coherence protocol. This "
            f"is not a configuration problem `run` can iterate its way past."
        ),
        next_steps=(
            "Adding real support for this core means writing new coherence-adapter RTL by "
            "hand (see how pico_l15_transducer.v does it for PicoRV32) -- a hardware design "
            "task, not something this loop automates. If an adapter already exists for this "
            "core elsewhere, extend chia_openpiton's PitonCore literal and sims_flags() the "
            "same way the pico core was added (docs/TECHNICAL_GUIDE.md section 7)."
        ),
    )


def format_report(session: Session, db: SQLiteNode | None = None) -> str:
    """The `.rpt`-style text `write_report` writes out, from whatever
    `run` last produced (a real LoopResult, or the static no-adapter case)."""
    lines = [f"MACE report -- top_module={session.top_module!r}, core_count={session.core_count}"]
    result = session.last_result
    if result is None:
        lines.append("(no run has happened yet in this session)")
        return "\n".join(lines) + "\n"
    lines.append(f"run_id: {getattr(result, 'run_id', '(none -- static check, no real run)')}")
    lines.append(f"status: {result.status}")
    if db is not None and getattr(result, "run_id", None):
        for key, value in summary(db, result.run_id).items():
            lines.append(f"  {key}: {value}")
    pm = result.post_mortem
    if pm is not None:
        lines.append(f"\nassessment: {pm.assessment}")
        lines.append(f"explanation: {pm.explanation}")
        if pm.next_steps:
            lines.append(f"next_steps: {pm.next_steps}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The interactive shell
# ---------------------------------------------------------------------------


class MaceShell(cmd.Cmd):
    intro = (
        "MACE interactive shell. read_verilog / top_module / read_spec / "
        "set_core, then run. Type help or ? for command details, exit to leave.\n"
    )
    prompt = "mace> "

    def __init__(self, session: Session, llm, db: SQLiteNode) -> None:
        super().__init__()
        self.session = session
        self.llm = llm
        self.db = db

    def do_read_verilog(self, arg: str) -> None:
        """read_verilog <file> [file2 ...] -- register RTL source files for the target core."""
        print(handle_read_verilog(self.session, arg))

    def do_top_module(self, arg: str) -> None:
        """top_module <name> -- declare the design's top-level module."""
        print(handle_top_module(self.session, arg))

    def do_read_spec(self, arg: str) -> None:
        """read_spec <file.txt> -- read the objective (and optionally workloads/core) from a text file."""
        print(handle_read_spec(self.session, arg))

    def do_set_core(self, arg: str) -> None:
        """set_core <N> -- target N total tiles (MACE picks a mesh shape and tells you what's known about it)."""
        print(handle_set_core(self.session, arg))

    def do_run(self, arg: str) -> None:
        """run [-verbose] -- execute against the accumulated session state."""
        # "logging should be verbose" is the project owner's own standing
        # instruction, not just an opt-in flag -- -verbose/--verbose are
        # accepted for EDA-tool familiarity but verbose is already the
        # session default (see Session.verbose).
        if self.session.top_module is not None and self.session.detected_core is None:
            pm = no_adapter_post_mortem(self.session)
            self.session.last_result = type(
                "StaticResult", (), {"run_id": None, "status": "no_adapter", "post_mortem": pm}
            )()
            print(f"ASSESSMENT: {pm.assessment}")
            print(f"EXPLANATION: {pm.explanation}")
            print(f"NEXT_STEPS: {pm.next_steps}")
            return

        spec = build_spec_from_session(self.session)
        print(f"Objective: {spec.objective}")
        print(f"Core: {spec.core}, mesh: {spec.target_mesh[0]}x{spec.target_mesh[1]}")
        print(f"Workloads: {', '.join(spec.workloads)}")

        def on_iteration(iteration, results):
            print(f"\n--- iteration {iteration} ---")
            for r in results:
                if r.task.kind == "config":
                    print(f"  [config] {r.task.spec}")
                    print(f"    build: {'OK' if r.build.success else 'FAILED'} ({r.build.wall_time_s:.0f}s)")
                    if not r.build.success:
                        print(f"    build stderr (tail):\n{r.build.stderr[-1000:]}")
                else:
                    print(f"  [verify] {r.task.spec}")
                    print(f"    build: {'OK' if r.build.success else 'FAILED'}")
                    if r.run is not None:
                        print(f"    verdict: {r.run.verdict}")
                        print("    --- verification log (tail) ---")
                        print(r.run.sim_log_tail[-1500:] if r.run.sim_log_tail else "(no sim log)")
                        if r.run.status_log:
                            print("    --- status.log ---")
                            print(r.run.status_log)

        result = run_mace_loop(
            (self.session.piton_root,), spec, self.llm, self.db, on_iteration=on_iteration
        )
        self.session.last_result = result
        print(f"\nrun_id={result.run_id} status={result.status}")
        if result.post_mortem is not None:
            pm = result.post_mortem
            print(f"ASSESSMENT: {pm.assessment}")
            print(f"EXPLANATION: {pm.explanation}")
            if pm.next_steps:
                print(f"NEXT_STEPS: {pm.next_steps}")

    def do_write_report(self, arg: str) -> None:
        """write_report [> ]<name>.rpt -- write the last run's report to a file."""
        target = arg.strip().lstrip(">").strip()
        if not target:
            print("ERROR: write_report needs a filename, e.g. write_report > result.rpt")
            return
        text = format_report(self.session, self.db)
        Path(target).write_text(text)
        print(f"wrote {target}")

    def do_exit(self, arg: str) -> bool:
        """exit -- leave the shell."""
        return True

    do_quit = do_exit

    def do_EOF(self, arg: str) -> bool:  # Ctrl-D
        print()
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@app.command()
def shell(
    piton_root: str = typer.Option(..., "--piton-root", help="OpenPiton checkout to work against"),
    db_path: str = typer.Option("runs/mace_cli.db", help="Metrics database path"),
) -> None:
    """Start the interactive shell (read_verilog, top_module, read_spec, set_core, run, write_report)."""
    config = load_config()
    if config is None:
        typer.echo("No credentials saved yet -- run `mace init` first.")
        raise typer.Exit(code=1)
    apply_config_to_environment(config)

    ray.init(address="local", resources={"openpiton": 1, f"{config['backend']}_creds": 1})
    llm = make_llm(config["backend"])
    db = open_db(os.path.abspath(db_path), ray_placement=False)
    session = Session(piton_root=piton_root)

    try:
        MaceShell(session, llm, db).cmdloop()
    finally:
        ray.shutdown()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
