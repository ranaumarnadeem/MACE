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
import shutil
import subprocess
import sys
from pathlib import Path

import ray
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from chia.database.sqlite_node import SQLiteNode
from chia_openpiton.parse import coverage_summary
from mace.cli.config import (
    BACKEND_ENV_VARS,
    DEFAULT_ENV_PATH,
    apply_env_to_environment,
    load_env_file,
    write_env_file,
)
from mace.cli.session import KNOWN_MESH_OUTCOMES, Session
from mace.cli.spec_file import parse_spec_file
from mace.llm import default_model_for_backend, make_llm
from mace.metrics import get_post_mortem, module_status, open_db, record_post_mortem, summary
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
    env_file: str = typer.Option(
        None, "--env-file", help="Where to write the .env file (default: ~/.mace/.env)"
    ),
) -> None:
    """Set up credentials and check the environment -- run this first."""
    path = write_env_file(backend, api_key, Path(env_file) if env_file else DEFAULT_ENV_PATH)
    env_var = BACKEND_ENV_VARS.get(backend, "MACE_LLM_API_KEY")
    typer.echo(f"Wrote {env_var} to {path} (owner-only permissions).")

    typer.echo("\nEnvironment checks:")
    all_ok = True
    for name, ok, detail in run_doctor_checks():
        mark = "OK  " if ok else "MISSING"
        typer.echo(f"  [{mark}] {name}: {detail}")
        all_ok = all_ok and ok
    if all_ok:
        typer.echo(
            f"\nEverything looks ready. Run:\n"
            f"  mace shell --piton-root /path/to/openpiton --api {path} --backend {backend}"
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
    if "core" in overrides:
        session.top_module = overrides["core"]
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
        coverage=session.coverage,
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
        statuses = module_status(db, result.run_id)
        if statuses:
            lines.append("\nper-module status:")
            for m in statuses:
                build_label = "OK" if m["build_success"] else "FAILED"
                lines.append(f"  {m['module']}: build {build_label} (task {m['task_id']}, iteration {m['iteration']})")
    if session.last_coverage is not None:
        cov = session.last_coverage
        if cov["percent"] is not None:
            lines.append(f"\ncoverage: {cov['percent']:.2f}% ({cov['hit']}/{cov['total']})")
        else:
            lines.append("\ncoverage: requested but report generation failed")
    pm = result.post_mortem
    if pm is not None:
        lines.append(f"\nassessment: {pm.assessment}")
        lines.append(f"explanation: {pm.explanation}")
        if pm.next_steps:
            lines.append(f"next_steps: {pm.next_steps}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Coverage -- real Verilator line coverage, proven end to end against real
# hardware in scripts/local_coverage_1x1_build_test.py before any of this
# was wired into the loop. See chia_openpiton.state_def.COVERAGE_LINE_FLAG's
# own docstring and that script's module docstring for the full account.
# ---------------------------------------------------------------------------


def find_coverage_dat(iterations) -> str | None:
    """The most recent real run_dir with a coverage.dat, from a completed
    loop's full iteration history, or None.

    Walks backward (latest iteration, latest task within it first) since a
    later coverage-enabled run's data supersedes an earlier one's."""
    for iteration in reversed(iterations):
        for r in reversed(iteration):
            if r.run is not None and r.run.run_dir:
                candidate = os.path.join(r.run.run_dir, "coverage.dat")
                if os.path.exists(candidate):
                    return candidate
    return None


def resolve_verilator_coverage() -> str:
    """Pick which verilator_coverage binary to run for annotation.

    Prefers whatever's first on PATH: this project now has multiple real
    provisioning paths (the Nix devShell, GCP's source build, a locally
    fixed worker env) that each pin a real, version-matched
    verilator_coverage, and using it keeps annotation self-consistent with
    whatever actually built the model -- coverage.dat's format is a
    Verilator-internal detail tied to the version that wrote it, not
    guaranteed stable across major versions, so a fixed binary can silently
    misread a coverage.dat some other version produced.

    Falls back to /usr/bin/verilator_coverage (a known-stable, if older,
    install -- see scripts/local_coverage_1x1_build_test.py's own account)
    when nothing resolves on PATH or the resolved binary faults on
    --version: confirmed directly that a bare conda-activated shell's own
    PATH still puts a broken system devel install ahead of the stable one,
    and this function runs in the user's own interactive CLI process, not
    inside a Ray worker -- it never inherits worker_env_commands' own
    PATH/VERILATOR_ROOT fix (cluster/local.yaml), only whatever the user's
    own shell happens to have.
    """
    candidate = shutil.which("verilator_coverage")
    if candidate:
        try:
            probe = subprocess.run(
                [candidate, "--version"], capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            probe = None
        if probe is not None and probe.returncode == 0:
            return candidate
    return "/usr/bin/verilator_coverage"


def generate_coverage_report(dat_path: str) -> tuple[dict, str]:
    """Run verilator_coverage --annotate on *dat_path*; returns (parsed
    summary dict, raw stdout).

    See resolve_verilator_coverage()'s own docstring for which binary runs
    and why.
    """
    annotate_dir = os.path.join(os.path.dirname(dat_path), "coverage_annotated")
    verilator_coverage = resolve_verilator_coverage()
    result = subprocess.run(
        [verilator_coverage, "--annotate", annotate_dir, dat_path],
        capture_output=True, text=True, timeout=120,
    )
    return coverage_summary(result.stdout), result.stdout


# ---------------------------------------------------------------------------
# Rich presentation helpers -- kept separate from the handle_* functions
# above on purpose: those stay plain-string-in, plain-string/dataclass-out so
# mace/test/test_cli.py can call them directly with no console attached.
# Only the shell's own do_* methods (below) render through Rich.
# ---------------------------------------------------------------------------

_ASSESSMENT_STYLE = {
    "fixable_config": "yellow",
    "likely_hardware_limitation": "red",
    "inconclusive": "cyan",
}

# (command, one-line description) for the custom `help` table -- pulled from
# each do_* method's own docstring at call time (see do_help), listed here
# only for display order. Deliberately excludes cmd.Cmd's own EOF/quit
# aliases -- exit is the one leave-the-shell command shown.
_HELP_ORDER = (
    "read_verilog",
    "top_module",
    "read_spec",
    "set_core",
    "run",
    "write_report",
    "help",
    "exit",
)


def _print_result(console: Console, msg: str) -> None:
    """Render a handle_*() result: red for an ERROR:-prefixed message, a
    green check for everything else."""
    if msg.startswith("ERROR"):
        console.print(f"[bold red]✗ {msg}[/bold red]")
    else:
        console.print(f"[green]✓[/green] {msg}")


def _print_post_mortem(console: Console, pm: PostMortem) -> None:
    style = _ASSESSMENT_STYLE.get(pm.assessment, "white")
    body = f"[bold]{pm.assessment}[/bold]\n\n{pm.explanation}"
    if pm.next_steps:
        body += f"\n\n[dim]Next steps:[/dim] {pm.next_steps}"
    console.print(Panel(body, title="Assessment", border_style=style, expand=False))


# ---------------------------------------------------------------------------
# The interactive shell
# ---------------------------------------------------------------------------


class MaceShell(cmd.Cmd):
    # Left unset (not cmd.Cmd's own `intro` attribute) so cmd.Cmd's own
    # cmdloop() doesn't ALSO print it -- unstyled, since cmd.Cmd's print path
    # doesn't know Rich markup -- on top of the styled one __init__ prints
    # below. Two prints of the same banner, one broken, was a real bug caught
    # by actually running this.
    intro = None
    rich_intro = (
        "[bold]MACE interactive shell.[/bold] read_verilog / top_module / "
        "read_spec / set_core, then run. Type [cyan]help[/cyan] for commands, "
        "[cyan]exit[/cyan] to leave."
    )
    # Rich can't style cmd.Cmd's plain input() prompt through markup, so this
    # is a raw ANSI escape (bold cyan) -- simpler and more reliable here than
    # fighting cmd.Cmd's own I/O assumptions for the one line that needs it.
    prompt = "\033[1;36mmace> \033[0m"

    def __init__(self, session: Session, llm, db: SQLiteNode) -> None:
        super().__init__()
        self.session = session
        self.llm = llm
        self.db = db
        # width=100: don't rely on terminal-size auto-detection, which is
        # unreliable when stdin/stdout aren't a real tty (piped input,
        # captured test output) and produced genuinely corrupted table
        # rendering under those conditions when this was actually run.
        self.console = Console(width=100)
        self.console.print(self.rich_intro)

    def onecmd(self, line: str) -> bool:
        # A live-tested bug: write_report on a directory that doesn't exist
        # raised FileNotFoundError straight through cmd.Cmd's own cmdloop(),
        # killing the whole process and losing every bit of session state
        # (read_verilog/top_module/set_core/run results) accumulated so far.
        # One bad command shouldn't be able to do that -- catch anything a
        # do_* method raises and report it the same way a handle_* ERROR
        # string already renders, so the shell (and the session) survives.
        try:
            return super().onecmd(line)
        except KeyboardInterrupt:
            # Ctrl-C during a long `run` (a real build/simulate can take
            # minutes) must return to the prompt, not kill the whole shell
            # and lose every bit of accumulated session state -- exactly
            # the case a user would want to interrupt. Exception alone
            # doesn't catch this: KeyboardInterrupt is a BaseException.
            # chia_openpiton._run already kills the underlying sims/
            # Verilator process group itself before this propagates, so
            # nothing is left running orphaned in the background.
            self.console.print("\n[bold yellow]✗ interrupted -- back to the prompt[/bold yellow]")
            return False
        except Exception as e:  # noqa: BLE001
            self.console.print(f"[bold red]✗ ERROR: {type(e).__name__}: {e}[/bold red]")
            return False

    def do_read_verilog(self, arg: str) -> None:
        """read_verilog <file> [file2 ...] -- register RTL source files for the target core."""
        _print_result(self.console, handle_read_verilog(self.session, arg))

    def do_top_module(self, arg: str) -> None:
        """top_module <name> -- declare the design's top-level module."""
        _print_result(self.console, handle_top_module(self.session, arg))

    def do_read_spec(self, arg: str) -> None:
        """read_spec <file.txt> -- read the objective (and optionally workloads/core) from a text file."""
        _print_result(self.console, handle_read_spec(self.session, arg))

    def do_set_core(self, arg: str) -> None:
        """set_core <N> -- target N total tiles (MACE picks a mesh shape and tells you what's known about it)."""
        _print_result(self.console, handle_set_core(self.session, arg))

    def do_run(self, arg: str) -> None:
        """run [-verbose] [-coverage] -- execute against the accumulated session state."""
        c = self.console
        # "logging should be verbose" is the project owner's own standing
        # instruction, not just an opt-in flag -- -verbose/--verbose are
        # accepted for EDA-tool familiarity but verbose is already the
        # session default (see Session.verbose).
        #
        # -coverage is real, unlike -verbose: it sets Session.coverage, which
        # sticks for future `run`s too (matching set_core's own
        # accumulates-until-changed convention), and actually changes what
        # gets built (chia_openpiton.state_def.COVERAGE_LINE_FLAG).
        tokens = shlex.split(arg) if arg.strip() else []
        recognized = {"-verbose", "--verbose", "-coverage", "--coverage"}
        unknown = [t for t in tokens if t not in recognized]
        if unknown:
            c.print(f"[bold red]✗ ERROR: unknown run option(s): {' '.join(unknown)}[/bold red]")
            return
        if "-coverage" in tokens or "--coverage" in tokens:
            self.session.coverage = True

        # Reset before either path below: a coverage report from a *previous*
        # run must never survive into this one's report, including the
        # no_adapter early-return -- write_report would otherwise print a
        # stale percentage next to an unrelated status.
        self.session.last_coverage = None

        if self.session.top_module is not None and self.session.detected_core is None:
            pm = no_adapter_post_mortem(self.session)
            self.session.last_result = type(
                "StaticResult", (), {"run_id": None, "status": "no_adapter", "post_mortem": pm}
            )()
            _print_post_mortem(c, pm)
            return

        spec = build_spec_from_session(self.session)
        c.print(
            f"[bold]Objective:[/bold] {spec.objective}\n"
            f"[bold]Core:[/bold] {spec.core}  [bold]Mesh:[/bold] "
            f"{spec.target_mesh[0]}x{spec.target_mesh[1]}\n"
            f"[bold]Workloads:[/bold] {', '.join(spec.workloads)}"
        )

        def on_iteration(iteration, results):
            # A "config" task builds one assembled-chip Verilator simulation
            # and stops there; a "workload" task builds and runs one gate
            # program against it, one pass/fail verdict. "unit_test" tasks
            # are the one per-module exception (see mace.loop.
            # _run_unit_test_step): each builds its own separate, smaller
            # testbench for one target module -- shown below, gated on build
            # success only, since running any of them still hits the same
            # documented test_infrstrct.v Verilator gap pico_reset_ut does
            # (see scripts/patch_openpiton.sh). (Real coverage -- how much of
            # the design a passing run actually exercised, per file -- is a
            # separate, real thing this shell now does, once, after the whole
            # run finishes; see below.) What this CAN always do, and a live
            # user correctly pointed out it wasn't doing: name the real files
            # on disk, and show much more than a fixed tail on failure, since
            # that's genuinely how someone would debug this by hand.
            c.rule(f"iteration {iteration}", style="cyan")
            for r in results:
                if r.task.kind == "config":
                    c.print(f"[yellow]Adding cache/config:[/yellow] {r.task.spec}")
                    status = "[green]OK[/green]" if r.build.success else "[bold red]FAILED[/bold red]"
                    c.print(f"  build: {status} ({r.build.wall_time_s:.0f}s)")
                    c.print(f"  [dim]model_dir: {r.build.model_dir}[/dim]")
                    if not r.build.success:
                        c.print(Panel(r.build.stderr[-4000:], title="build stderr (tail)", border_style="red"))
                        c.print(f"  [dim]full stderr is on the build artifact; model_dir above has sims.log[/dim]")
                elif r.task.kind == "unit_test":
                    c.print(f"[yellow]Unit test:[/yellow] {r.task.spec}")
                    status = "[green]OK[/green]" if r.build.success else "[bold red]FAILED[/bold red]"
                    c.print(f"  build: {status} ({r.build.wall_time_s:.0f}s)")
                    c.print(f"  [dim]model_dir: {r.build.model_dir}[/dim]")
                    if not r.build.success:
                        c.print(Panel(r.build.stderr[-4000:], title="build stderr (tail)", border_style="red"))
                    else:
                        c.print(
                            "  [dim]run skipped -- test_infrstrct.v's Verilator "
                            "incompatibility blocks running any unit-test environment "
                            "for now (see scripts/patch_openpiton.sh); build success "
                            "is this task's gate.[/dim]"
                        )
                else:
                    c.print(f"[yellow]Running verification:[/yellow] {r.task.spec}")
                    status = "[green]OK[/green]" if r.build.success else "[bold red]FAILED[/bold red]"
                    c.print(f"  build: {status}")
                    if r.run is not None:
                        v_style = "green" if r.run.verdict == "pass" else "red"
                        c.print(f"  verdict: [bold {v_style}]{r.run.verdict}[/bold {v_style}]")
                        c.print(f"  [dim]run_dir: {r.run.run_dir}[/dim]  (full sim.log, status.log, fake_uart.log all live here)")
                        passed = r.run.verdict == "pass"
                        tail_chars = 1500 if passed else 8000
                        log = r.run.sim_log_tail[-tail_chars:] if r.run.sim_log_tail else "(no sim log)"
                        title = "verification log (tail)" if passed else "verification log (tail -- see run_dir above for the full file)"
                        c.print(Panel(log, title=title, border_style="dim" if passed else "red"))
                        if r.run.status_log:
                            c.print(Panel(r.run.status_log, title="status.log", border_style="dim"))
                    elif not r.build.success:
                        c.print(f"  [dim]never reached run -- build failed, see stderr above[/dim]")

        result = run_mace_loop(
            (self.session.piton_root,), spec, self.llm, self.db, on_iteration=on_iteration
        )
        self.session.last_result = result
        status_style = "green" if result.status == "passed" else "red"
        c.print(f"\nrun_id=[bold]{result.run_id}[/bold] status=[bold {status_style}]{result.status}[/bold {status_style}]")
        if result.post_mortem is not None:
            _print_post_mortem(c, result.post_mortem)

        if result.run_id is not None:
            statuses = module_status(self.db, result.run_id)
            if statuses:
                table = Table(title="per-module status", header_style="bold cyan", show_lines=False)
                table.add_column("module", style="bold")
                table.add_column("build")
                table.add_column("task")
                table.add_column("iteration")
                for m in statuses:
                    build_cell = "[green]OK[/green]" if m["build_success"] else "[bold red]FAILED[/bold red]"
                    table.add_row(m["module"], build_cell, m["task_id"], str(m["iteration"]))
                c.print(table)

        if self.session.coverage and result.status == "passed":
            dat_path = find_coverage_dat(result.iterations)
            if dat_path is None:
                c.print("[dim]coverage was requested but no coverage.dat was found (unexpected)[/dim]")
            else:
                c.print("[yellow]Generating coverage report...[/yellow]")
                cov, raw = generate_coverage_report(dat_path)
                self.session.last_coverage = cov
                if cov["percent"] is not None:
                    title = f"coverage: {cov['percent']:.2f}% ({cov['hit']}/{cov['total']})"
                    c.print(Panel(raw.strip(), title=title, border_style="cyan"))
                else:
                    c.print("[bold red]✗ coverage report generation failed[/bold red]")
                    c.print(Panel(raw[-2000:] or "(no output)", title="verilator_coverage output", border_style="red"))

    def do_write_report(self, arg: str) -> None:
        """write_report [> ]<name>.rpt -- write the last run's report to a file."""
        target = arg.strip().lstrip(">").strip()
        if not target:
            self.console.print("[bold red]✗ ERROR: write_report needs a filename, e.g. write_report > result.rpt[/bold red]")
            return
        text = format_report(self.session, self.db)
        Path(target).write_text(text)
        self.console.print(f"[green]✓[/green] wrote {target}")

    def do_help(self, arg: str) -> None:
        """help [command] -- list commands, or show one command's full docstring."""
        if arg:
            doc = (getattr(self, f"do_{arg}", None) or (lambda a: None)).__doc__
            if doc:
                self.console.print(doc.strip())
            else:
                self.console.print(f"[red]no such command: {arg}[/red]")
            return
        table = Table(title="MACE shell commands", header_style="bold cyan", show_lines=False)
        table.add_column("command", style="bold")
        table.add_column("description")
        for name in _HELP_ORDER:
            method = getattr(self, f"do_{name}", None)
            doc = (method.__doc__ or "").strip().split("\n")[0]
            # Each docstring is "name <args> -- description"; show only the
            # description half here, the table's own column already has the name.
            desc = doc.split("--", 1)[1].strip() if "--" in doc else doc
            table.add_row(name, desc)
        self.console.print(table)

    do_h = do_help

    def do_exit(self, arg: str) -> bool:
        """exit -- leave the shell."""
        return True

    do_quit = do_exit

    def do_EOF(self, arg: str) -> bool:  # Ctrl-D
        self.console.print()
        return True

    def default(self, line: str) -> None:
        # Two real mistakes a live user made back to back: typing `init` (a
        # top-level `mace` command that sets up credentials *before* the
        # shell starts, not a shell command) and typing `mace init ...`
        # out of habit while still inside the shell. A generic "unknown
        # command" left them guessing both times -- name the actual fix.
        word = line.split()[0] if line.split() else ""
        if word == "init":
            self.console.print(
                "[yellow]'init' sets up credentials before the shell starts -- it isn't a "
                "shell command.[/yellow] Exit first ([cyan]exit[/cyan]), then from your regular "
                "terminal run:\n  [bold]mace init --backend opencode --api-key <key>[/bold]\n"
                "then relaunch pointing at the .env file it writes:\n"
                "  [bold]mace shell --piton-root ... --api ~/.mace/.env[/bold]"
            )
            return
        if word == "mace":
            self.console.print(
                "[yellow]You're already inside the mace shell -- drop the `mace` prefix.[/yellow] "
                "e.g. type [bold]run[/bold], not [dim]mace run[/dim]."
            )
            return
        self.console.print(f"[red]unknown command: {word or line!r}[/red] (type [cyan]help[/cyan] for the list)")

    def emptyline(self) -> None:
        pass  # cmd.Cmd's default re-runs the last command on a blank line -- surprising here


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_ADC_BACKENDS = frozenset({"vertex"})  # authenticate via Google ADC, not a literal key string


@app.command()
def shell(
    piton_root: str = typer.Option(..., "--piton-root", help="OpenPiton checkout to work against"),
    api: str = typer.Option(
        None,
        "--api",
        help="Path to a .env file with the backend's API key. Required for opencode/claude/"
        "antigravity; omit for vertex if `gcloud auth application-default login` is already set up.",
    ),
    backend: str = typer.Option(
        "vertex", "--backend",
        help="LLM backend: vertex (default -- Gemini on GCP, this project's funded "
        "credits), opencode, claude, antigravity (no credits available for these -- "
        "see mace.llm's own docstring).",
    ),
    model: str = typer.Option(
        None, "--model",
        help="Model name (sets MACE_LLM_MODEL). Defaults to gemini-2.5-flash "
        "(confirmed reachable on this project's GCP project as of 2026-09-19) "
        "when backend=vertex and no model is given; other backends use their "
        "own default model unless one is given explicitly here.",
    ),
    db_path: str = typer.Option("runs/mace_cli.db", help="Metrics database path"),
) -> None:
    """Start the interactive shell (read_verilog, top_module, read_spec, set_core, run, write_report)."""
    # --api is mandatory for a literal-API-key backend, not auto-loaded from a
    # saved file: a live user hit a stale key silently pulled in from disk
    # with no visibility into what was actually being used. Naming the file
    # explicitly on every launch means `cat` on that exact path always tells
    # you what's in play. vertex is the one exception: Google ADC
    # (gcloud auth application-default login) authenticates with no key
    # string anywhere, so requiring --api there would demand a file that has
    # nothing real to put in it.
    if api is not None:
        try:
            env = load_env_file(api)
        except FileNotFoundError:
            typer.echo(f"No .env file at {api!r} -- run `mace init` first, or check the path.")
            raise typer.Exit(code=1)
        apply_env_to_environment(env)
        expected_var = BACKEND_ENV_VARS.get(backend, "MACE_LLM_API_KEY")
        if expected_var not in env:
            typer.echo(
                f"{api} doesn't set {expected_var}, which the {backend!r} backend needs -- "
                f"check the file or your --backend value."
            )
            raise typer.Exit(code=1)
    elif backend not in _ADC_BACKENDS:
        typer.echo(
            f"--api is required for backend={backend!r} -- it needs a real API key. "
            f"Only vertex can omit it, and only once `gcloud auth application-default "
            f"login` has been run."
        )
        raise typer.Exit(code=1)

    model = default_model_for_backend(model, backend)
    if model:
        os.environ["MACE_LLM_MODEL"] = model
    elif backend == "vertex" and "MACE_LLM_MODEL" not in os.environ:
        typer.echo("--model is required for backend='vertex' (e.g. --model gemini-2.0-flash-001).")
        raise typer.Exit(code=1)
    os.environ.setdefault("MACE_LLM", backend)
    if backend == "vertex":
        # VertexGeminiLLM reads GOOGLE_CLOUD_PROJECT itself; nothing else in
        # this path sets it. Confirmed real (gcloud's own configured
        # project, reachable via the Vertex REST API) rather than guessed.
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "mace-508004")

    ray.init(address="local", resources={"openpiton": 1, f"{backend}_creds": 1})
    llm = make_llm(backend)
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
