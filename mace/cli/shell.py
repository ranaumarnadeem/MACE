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
import difflib
import glob as _glob
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import readline  # not available on native Windows without WSL/a shim
except ImportError:
    readline = None

import ray
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from chia.database.sqlite_node import SQLiteNode
from chia_openpiton.parse import coverage_summary
from mace.cli.config import (
    BACKEND_ENV_VARS,
    CONFIG_DIR,
    DEFAULT_ENV_PATH,
    apply_env_to_environment,
    load_env_file,
    write_env_file,
)
from mace.cli.session import KNOWN_MESH_OUTCOMES, Session
from mace.cli.spec_file import parse_spec_file
from mace.llm import default_model_for_backend, make_llm
from mace.metrics import (
    all_runs,
    failure_taxonomy,
    get_post_mortem,
    module_status,
    open_db,
    record_post_mortem,
    summary,
    trace_run,
)
from mace.orchestrator import run_mace_loop
from mace.spec import Budget, MaceSpec, PostMortem
from mace.workloads import RECOMMENDED_RTL_TIMEOUT

app = typer.Typer(help="MACE: point it at a core, tell it what to verify, and watch it work.")


def _is_windows() -> bool:
    """A thin wrapper over ``os.name == "nt"`` so tests can monkeypatch
    platform-specific branches (see ``_split_file_args``/``_gcloud_adc_path``)
    without setting the real, process-global ``os.name`` -- several stdlib
    modules (``pathlib`` chief among them) read that directly, and doing so
    was confirmed to break things: it made ``pathlib.Path`` try to
    instantiate a real ``WindowsPath`` later in the same process, crashing
    pytest's own cache-writing teardown with a raw ``NotImplementedError``.
    """
    return os.name == "nt"


def _gcloud_adc_path() -> Path:
    """Where `gcloud auth application-default login` writes its credentials
    file. gcloud itself only uses ``~/.config/gcloud`` on Linux/macOS --
    on Windows it writes under ``%APPDATA%\\gcloud`` instead, so checking
    the POSIX path there always reports MISSING even right after a
    successful login.
    """
    if _is_windows() and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "gcloud" / "application_default_credentials.json"
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


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
    # vertex, matching `shell`'s own default: this project's only funded
    # backend, so a first-time user following bare defaults on both
    # commands ends up configured for the one that actually works, not one
    # documented as unfunded (opencode/claude/antigravity).
    backend: str = typer.Option("vertex", help="LLM backend: vertex, opencode, claude, antigravity"),
    api_key: str = typer.Option(
        None,
        help="API key/credential for the chosen backend; prompted for "
        "interactively if omitted. Ignored for vertex, which authenticates "
        "via Google ADC instead -- see below.",
    ),
    env_file: str = typer.Option(
        None, "--env-file", help="Where to write the .env file (default: ~/.mace/.env)"
    ),
) -> None:
    """Set up credentials and check the environment -- run this first."""
    if backend == "vertex":
        # vertex authenticates via `gcloud auth application-default login`
        # (ADC), which needs no key string anywhere -- unlike every other
        # backend here. Prompting for one the same way would write a
        # real-looking but never-read value (GOOGLE_APPLICATION_CREDENTIALS
        # is a path to a service-account file, not a pasted secret, and ADC
        # doesn't need that env var set at all) into a .env file that then
        # silently does nothing -- a real live-user rough edge. No .env file
        # is written for this backend.
        adc_file = _gcloud_adc_path()
        typer.echo("backend=vertex authenticates via Google ADC, not a stored key/file.")
        if adc_file.exists():
            typer.echo(f"  [OK] Application Default Credentials found at {adc_file}")
        else:
            typer.echo(
                f"  [MISSING] {adc_file} not found -- run this once:\n"
                f"    gcloud auth application-default login"
            )
        run_command = "mace shell --piton-root /path/to/openpiton --backend vertex"
    else:
        if api_key is None:
            api_key = typer.prompt("Api key", hide_input=True)
        path = write_env_file(backend, api_key, Path(env_file) if env_file else DEFAULT_ENV_PATH)
        env_var = BACKEND_ENV_VARS.get(backend, "MACE_LLM_API_KEY")
        typer.echo(f"Wrote {env_var} to {path} (owner-only permissions).")
        run_command = f"mace shell --piton-root /path/to/openpiton --api {path} --backend {backend}"

    typer.echo("\nEnvironment checks:")
    all_ok = True
    for name, ok, detail in run_doctor_checks():
        mark = "OK  " if ok else "MISSING"
        typer.echo(f"  [{mark}] {name}: {detail}")
        all_ok = all_ok and ok
    if all_ok:
        typer.echo(f"\nEverything looks ready. Run:\n  {run_command}")
    else:
        typer.echo(
            "\nSome tools are missing -- `read_verilog`/`run` may fail until "
            "they're on PATH. See docs/TECHNICAL_GUIDE.md section 9 for setup.\n"
            f"Run:\n  {run_command}"
        )


# ---------------------------------------------------------------------------
# Command handlers -- pure(ish) logic, no cmd.Cmd/I/O dependency.
# ---------------------------------------------------------------------------


def _split_file_args(arg: str) -> list[str]:
    """shlex.split, but safe for a native Windows path.

    shlex's default POSIX mode treats ``\\`` as an escape character, so a
    path like ``C:\\Users\\me\\foo.v`` comes out mangled to
    ``C:Usersmefoo.v`` -- every backslash silently eaten. Non-POSIX mode
    keeps backslashes literal (right for a Windows path), at the cost of
    leaving wrapping quotes attached to each token, which are stripped
    back off here to match POSIX mode's own behavior.
    """
    if not _is_windows():
        return shlex.split(arg)
    tokens = shlex.split(arg, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]


def handle_read_verilog(session: Session, arg: str) -> str:
    """`read_verilog <file> [file2 ...]` -- check that RTL source files exist.

    This is validation only: chia_openpiton's build has no "extra source
    files" concept for build_spec_from_session/run to feed these into, so
    unlike Yosys/OpenROAD's own read_verilog, nothing here actually adds
    these files to what `run` builds.
    """
    if not arg.strip():
        return "ERROR: read_verilog needs at least one file"
    try:
        tokens = _split_file_args(arg)
    except ValueError as e:
        # An unterminated quote (read_verilog "foo.v) -- caught here so it
        # gets this module's own clean ERROR: message, not the shell's
        # generic exception handler's raw "ValueError: ..." text.
        return f"ERROR: {e}"
    files = [Path(f) for f in tokens]
    missing = [f for f in files if not f.exists()]
    if missing:
        return "ERROR: file(s) not found: " + ", ".join(str(f) for f in missing)
    session.verilog_files = tuple(files)
    return (
        f"found {len(files)} file(s) (validated only, not wired into the build): "
        + ", ".join(f.name for f in files)
    )


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
    # getattr's own default only fires when run_id is *missing*; the static
    # no-adapter result sets it explicitly to None (present, not absent), so
    # that case needs its own check or the report prints the literal
    # "run_id: None" instead of this friendlier placeholder.
    run_id = getattr(result, "run_id", None)
    lines.append(f"run_id: {run_id if run_id is not None else '(none -- static check, no real run)'}")
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

# readline history, persisted across shell restarts (MaceShell.preloop/
# postloop) -- next to the other ~/.mace/ state (.env), not lost the moment
# the process exits the way cmd.Cmd's own in-session-only history is.
HISTORY_FILE = CONFIG_DIR / ".shell_history"

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


def _complete_path(text: str) -> list[str]:
    """Filesystem completions for *text*, the partial path already typed --
    a directory gets a trailing separator so tab-completion can keep
    descending into it, matching a real shell's own convention. Best-effort:
    an unreadable path segment just yields no completions rather than
    raising into readline's own completer, which cmd.Cmd has no clean way
    to recover from mid-keystroke.
    """
    try:
        matches = _glob.glob(text + "*")
    except OSError:
        return []
    return [m + os.sep if os.path.isdir(m) else m for m in matches]


def _print_result(console: Console, msg: str) -> None:
    """Render a handle_*() result: red for an ERROR:-prefixed message, a
    green check for everything else.

    escape(): every handle_* message embeds the user's own raw argument
    (a filename, a module name, ...) -- unescaped, typing something merely
    shaped like "[...]" (e.g. `top_module notes[/legacy]`) raises an
    uncaught rich.errors.MarkupError instead of printing the literal text,
    crashing the interactive shell on an otherwise ordinary command.
    """
    if msg.startswith("ERROR"):
        console.print(f"[bold red]✗ {escape(msg)}[/bold red]")
    else:
        console.print(f"[green]✓[/green] {escape(msg)}")


def parse_script_lines(text: str) -> list[str]:
    """Executable command lines from a script file's text -- blank lines and
    full-line ``#`` comments are dropped, matching Yosys's own
    ``-c script.ys`` convention (see MaceShell.run_script). cmd.Cmd's own
    line dispatch has no comment syntax -- a literal ``#`` line left in
    would hit `default()` as an "unknown command" instead of being ignored.
    """
    return [
        line
        for line in (raw.strip() for raw in text.splitlines())
        if line and not line.startswith("#")
    ]


def _print_post_mortem(console: Console, pm: PostMortem) -> None:
    # escape(): explanation/next_steps are real LLM free text -- see
    # _print_result's own comment on why unescaped markup-shaped text
    # crashes instead of printing literally.
    style = _ASSESSMENT_STYLE.get(pm.assessment, "white")
    body = f"[bold]{escape(pm.assessment)}[/bold]\n\n{escape(pm.explanation)}"
    if pm.next_steps:
        body += f"\n\n[dim]Next steps:[/dim] {escape(pm.next_steps)}"
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
    # Class-level fallback for non-tty output (piped, redirected, captured);
    # __init__ overrides it with the real escape when stdout is a tty --
    # unlike every other line here, this one bypasses Rich, which guards
    # against exactly this on its own.
    prompt = "mace> "

    def __init__(self, session: Session, llm, db: SQLiteNode) -> None:
        super().__init__()
        self.session = session
        self.llm = llm
        self.db = db
        if sys.stdout.isatty():
            self.prompt = "\033[1;36mmace> \033[0m"
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
            # escape(str(e)): an exception message can embed arbitrary
            # user-supplied text (e.g. a bad path) -- same markup-crash
            # risk as _print_result's own free-text fields.
            self.console.print(f"[bold red]✗ ERROR: {type(e).__name__}: {escape(str(e))}[/bold red]")
            return False

    def preloop(self) -> None:
        # cmd.Cmd's own hook, called once before cmdloop's own input loop
        # starts. Loads readline history from a previous session, if any --
        # readline already gives up/down-arrow recall within one session on
        # its own; this is what makes it survive a restart.
        if readline is not None:
            try:
                readline.read_history_file(HISTORY_FILE)
            except OSError:
                pass  # first run, or an unreadable/missing history file

    def postloop(self) -> None:
        # Counterpart to preloop, called once as cmdloop's own input loop
        # ends (exit/EOF/an uncaught exception propagating past onecmd).
        if readline is not None:
            try:
                HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                readline.write_history_file(HISTORY_FILE)
            except OSError:
                pass

    def do_read_verilog(self, arg: str) -> None:
        """read_verilog <file> [file2 ...] -- check that RTL source files exist (validation only; run doesn't build them)."""
        _print_result(self.console, handle_read_verilog(self.session, arg))

    def complete_read_verilog(self, text, line, begidx, endidx):
        return _complete_path(text)

    def do_top_module(self, arg: str) -> None:
        """top_module <name> -- declare the design's top-level module."""
        _print_result(self.console, handle_top_module(self.session, arg))

    def do_read_spec(self, arg: str) -> None:
        """read_spec <file.txt> -- read the objective (and optionally workloads/core) from a text file."""
        _print_result(self.console, handle_read_spec(self.session, arg))

    def complete_read_spec(self, text, line, begidx, endidx):
        return _complete_path(text)

    def do_set_core(self, arg: str) -> None:
        """set_core <N> -- target N total tiles (MACE picks a mesh shape and tells you what's known about it)."""
        _print_result(self.console, handle_set_core(self.session, arg))

    def do_run(self, arg: str) -> None:
        """run [-verbose] [-coverage] -- execute against the accumulated session
        state. -verbose is accepted for Yosys/OpenROAD-style familiarity but
        has no effect: output is always verbose here (a standing project
        decision, not a togglable default) -- see the note this command
        prints if you pass it."""
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
        try:
            tokens = shlex.split(arg) if arg.strip() else []
        except ValueError as e:
            # An unterminated quote -- caught here so it gets this command's
            # own clean ERROR: message, not the shell's generic exception
            # handler's raw "ValueError: ..." text.
            c.print(f"[bold red]✗ ERROR: {e}[/bold red]")
            return
        recognized = {"-verbose", "--verbose", "-coverage", "--coverage"}
        unknown = [t for t in tokens if t not in recognized]
        if unknown:
            if ">" in unknown:
                # write_report's own `> file` syntax doesn't generalize here
                # -- a plausible mistake right after learning it, since this
                # is the one other command that produces output. Name the
                # actual fix instead of a generic "unknown option".
                c.print(
                    "[bold red]✗ ERROR: run doesn't support `>` redirection -- its "
                    "progress and results always print to this console.[/bold red]\n"
                    "[dim]Run it plainly, then save the report afterward: "
                    "write_report > result.rpt[/dim]"
                )
            else:
                c.print(f"[bold red]✗ ERROR: unknown run option(s): {' '.join(unknown)}[/bold red]")
            return
        if "-verbose" in tokens or "--verbose" in tokens:
            c.print("[dim]-verbose has no effect: output here is always verbose.[/dim]")
        if "-coverage" in tokens or "--coverage" in tokens:
            self.session.coverage = True

        # last_result and last_coverage are always set together below, at the
        # point a new result actually exists -- never reset ahead of time.
        # An earlier version cleared last_coverage here, before
        # build_spec_from_session/run_mace_loop even ran; an interrupt or
        # exception between that reset and the eventual `last_result =`
        # assignment (e.g. Ctrl-C mid-run) left last_coverage wiped while
        # last_result still held a PREVIOUS run's data -- write_report would
        # then print that old run's status with no coverage, even if that
        # old run genuinely had a coverage percentage to show.
        if self.session.top_module is not None and self.session.detected_core is None:
            pm = no_adapter_post_mortem(self.session)
            self.session.last_result = type(
                "StaticResult", (), {"run_id": None, "status": "no_adapter", "post_mortem": pm}
            )()
            self.session.last_coverage = None  # this static result never has one
            _print_post_mortem(c, pm)
            return

        spec = build_spec_from_session(self.session)
        c.print(
            f"[bold]Objective:[/bold] {spec.objective}\n"
            f"[bold]Core:[/bold] {spec.core}  [bold]Mesh:[/bold] "
            f"{spec.target_mesh[0]}x{spec.target_mesh[1]}\n"
            f"[bold]Workloads:[/bold] {', '.join(spec.workloads)}"
        )

        def on_task_progress(task_ids, stage):
            # Real-time in-flight feedback: on_iteration below only fires
            # once an ENTIRE iteration (every level, every batch) is done,
            # so without this a multi-minute build/run leaves the user
            # staring at a silent terminal with no way to tell "working"
            # from "stuck". Fires right before each dispatch that can
            # genuinely take a while -- see integrate_parallel's docstring.
            c.print(f"[dim cyan]  {stage} {', '.join(task_ids)}...[/dim cyan]")

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
            (self.session.piton_root,), spec, self.llm, self.db,
            on_iteration=on_iteration, on_task_progress=on_task_progress,
        )
        self.session.last_result = result
        self.session.last_coverage = None  # overwritten below if coverage was requested
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
                    table.add_row(escape(m["module"]), build_cell, escape(m["task_id"]), str(m["iteration"]))
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
        self.console.print(f"[green]✓[/green] wrote {escape(target)}")

    def complete_write_report(self, text, line, begidx, endidx):
        return _complete_path(text)

    def do_help(self, arg: str) -> None:
        """help [command] -- list commands, or show one command's full docstring."""
        if arg:
            doc = (getattr(self, f"do_{arg}", None) or (lambda a: None)).__doc__
            if doc:
                # escape(): several do_* docstrings document optional args
                # as "[file2 ...]" -- unescaped, Rich's markup parser
                # silently drops that exact text instead of printing it.
                self.console.print(escape(doc.strip()))
            else:
                self.console.print(f"[red]no such command: {escape(arg)}[/red]")
            return
        table = Table(title="MACE shell commands", header_style="bold cyan", show_lines=False)
        table.add_column("command", style="bold")
        table.add_column("usage", style="dim")
        table.add_column("description")
        for name in _HELP_ORDER:
            method = getattr(self, f"do_{name}", None)
            doc = (method.__doc__ or "").strip().split("\n")[0]
            # Each docstring is "name <args> -- description" -- split it into
            # its own usage and description columns rather than showing only
            # the description here and making a user run `help <command>`
            # just to learn the argument syntax.
            if "--" in doc:
                before, desc = doc.split("--", 1)
                usage = before[len(name):].strip() if before.startswith(name) else before.strip()
                desc = desc.strip()
            else:
                usage, desc = "", doc
            # escape(): usage/desc come straight from a docstring's own
            # "[optional arg]" syntax, e.g. read_verilog's "[file2 ...]" --
            # unescaped, Rich's markup parser silently drops that text.
            table.add_row(name, escape(usage), escape(desc))
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
        # Generic closest-match suggestion for anything else mistyped --
        # difflib is nearly free against this shell's own small (~10)
        # command vocabulary, and a real typo is a more common mistake than
        # the two hand-diagnosed ones above.
        commands = [name[len("do_"):] for name in self.get_names() if name.startswith("do_")]
        close = difflib.get_close_matches(word, commands, n=1)
        if close:
            self.console.print(
                f"[red]unknown command: {escape(repr(word))}[/red] -- did you mean "
                f"[bold]{close[0]}[/bold]? (type [cyan]help[/cyan] for the list)"
            )
            return
        self.console.print(
            f"[red]unknown command: {escape(repr(word or line))}[/red] "
            "(type [cyan]help[/cyan] for the list)"
        )

    def emptyline(self) -> None:
        pass  # cmd.Cmd's default re-runs the last command on a blank line -- surprising here

    def run_script(self, lines: list[str]) -> None:
        """Non-interactive counterpart to cmdloop() -- run each line through
        onecmd() in order, exactly like a typed interactive session, just
        read from a file instead of stdin (Yosys's own ``-c script.ys``
        convention). Stops early only if a command signals STOP (exit/EOF),
        matching cmd.Cmd's own convention; any other error is reported and
        execution continues to the next line, the same as onecmd's own
        interactive error handling already does.
        """
        for line in lines:
            # escape(): line is raw text from the script file -- unescaped,
            # a line merely containing something bracket-shaped (e.g. a
            # path like "notes[/legacy].txt") is either silently mangled
            # by Rich's markup parser or raises MarkupError outright,
            # aborting the whole script before onecmd ever sees it.
            self.console.print(f"{self.prompt}{escape(line)}")
            line = self.precmd(line)
            stop = self.onecmd(line)
            stop = self.postcmd(stop, line)
            if stop:
                break


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
        help="LLM backend: vertex (default, Gemini on Vertex AI), opencode, claude, or antigravity.",
    ),
    model: str = typer.Option(
        None, "--model",
        help="Model name (sets MACE_LLM_MODEL). Defaults to gemini-2.5-flash when "
        "backend=vertex; other backends use their own default unless one is given here.",
    ),
    db_path: str = typer.Option("runs/mace_cli.db", help="Metrics database path"),
    script: str = typer.Option(
        None, "--script", "-c",
        help="Run commands from this file non-interactively instead of starting the "
        "REPL (Yosys's own -c convention; blank lines and full-line # comments are "
        "skipped -- see MaceShell.run_script).",
    ),
) -> None:
    """Start the interactive shell (read_verilog, top_module, read_spec, set_core, run, write_report)."""
    # Checked up front: a wrong path would otherwise only fail at `run`.
    if not os.path.isdir(os.path.join(piton_root, "piton")):
        typer.echo(f"--piton-root {piton_root!r} is not an OpenPiton checkout (no piton/ directory).")
        raise typer.Exit(code=1)
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
    if backend == "vertex" and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        # VertexGeminiLLM reads the project from this variable, and nothing
        # here sets a default: the project that pays for the calls is yours.
        typer.echo(
            "--backend vertex needs GOOGLE_CLOUD_PROJECT set to your GCP project "
            "(export GOOGLE_CLOUD_PROJECT=<project>)."
        )
        raise typer.Exit(code=1)

    model = default_model_for_backend(model, backend)
    if model:
        os.environ["MACE_LLM_MODEL"] = model
    elif backend == "vertex" and "MACE_LLM_MODEL" not in os.environ:
        typer.echo("--model is required for backend='vertex' (e.g. --model gemini-2.0-flash-001).")
        raise typer.Exit(code=1)
    os.environ.setdefault("MACE_LLM", backend)

    script_lines = None
    if script is not None:
        # Fail fast, before touching Ray or the LLM backend -- same
        # convention --api's own missing-file check follows above.
        try:
            script_lines = parse_script_lines(Path(script).read_text())
        except OSError as e:
            typer.echo(f"Can't read script file {script!r}: {e}")
            raise typer.Exit(code=1)

    ray.init(address="local", resources={"openpiton": 1, f"{backend}_creds": 1})
    llm = make_llm(backend)
    db = open_db(os.path.abspath(db_path), ray_placement=False)
    session = Session(piton_root=piton_root)

    try:
        shell_obj = MaceShell(session, llm, db)
        if script_lines is not None:
            shell_obj.run_script(script_lines)
        else:
            shell_obj.cmdloop()
    finally:
        ray.shutdown()


cluster_app = typer.Typer(
    help="Thin wrapper over chia up/down and ray status -- see "
    "docs/TECHNICAL_GUIDE.md section 9 for what these actually do."
)
app.add_typer(cluster_app, name="cluster")


def _run_chia(cmd: list[str]) -> None:
    """Shell out and exit with the child's own exit code -- a pass-through,
    not a reimplementation, so chia up/down's own prompts, errors, and exit
    codes are exactly what the user sees, not a MACE-specific paraphrase of
    them.

    The one case that isn't the child's own output: ``cmd[0]`` (``chia`` or
    ``ray``) missing from PATH entirely -- subprocess.run can't hand us a
    child exit code for a process that never started, so this mirrors
    chia's own ray_passthrough.py convention for the identical situation
    (friendly message on stderr, exit 127) instead of letting a raw
    FileNotFoundError traceback crash the whole command.
    """
    try:
        result = subprocess.run(cmd)
    except FileNotFoundError:
        typer.echo(f"mace: {cmd[0]!r} was not found on PATH.", err=True)
        raise typer.Exit(code=127)
    raise typer.Exit(code=result.returncode)


@cluster_app.command("up")
def cluster_up(
    config_file: str = typer.Argument(..., help="Cluster YAML config, e.g. cluster/local.yaml"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip chia up's interactive confirmation"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan without provisioning anything"),
) -> None:
    """Bring up a cluster. Cloud nodes are real, billed compute -- read
    docs/TECHNICAL_GUIDE.md section 9.3 before running this against GCP."""
    cmd = ["chia", "up", config_file]
    if yes:
        cmd.append("--yes")
    if dry_run:
        cmd.append("--dry-run")
    _run_chia(cmd)


@cluster_app.command("down")
def cluster_down(
    config_file: str = typer.Argument(..., help="Cluster YAML config, e.g. cluster/local.yaml"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip chia down's interactive confirmation"),
) -> None:
    """Tear down a cluster. Always verify afterward (e.g. `gcloud compute
    instances list`) -- never assume a teardown succeeded."""
    cmd = ["chia", "down", config_file]
    if yes:
        cmd.append("--yes")
    _run_chia(cmd)


@cluster_app.command("status")
def cluster_status() -> None:
    """Proxy to `ray status` for whatever cluster this machine is currently
    connected to (chia promotes this exact command -- see chia.cli.main)."""
    _run_chia(["ray", "status"])


def _render_trace(console: Console, t: dict) -> None:
    """The plan -> dispatch -> triage story for one run as a tree: each
    iteration is a replan cycle, its tasks are what got dispatched, and a
    TRIAGE branch appears only when something needed diagnosing -- absence
    of one is itself the answer to "did this iteration need a replan".

    Every interpolated field that isn't a guaranteed-safe literal (task
    ids, module names, and especially diagnosis/fix/objective -- real LLM
    free text, see mace.metrics.failure_taxonomy's own docstring) is
    escaped with rich.markup.escape() first: unescaped, a value containing
    something that merely looks like a markup tag (e.g. "see [l1d_size]")
    is silently corrupted or dropped by Rich's own markup parser instead
    of printed as the literal text it actually is.
    """
    tree = Tree(
        f"[bold]{escape(t['run_id'])}[/bold] -- {escape(t['core'])} {escape(t['mesh'])} -- "
        f"{escape(repr(t['objective']))} -- status={escape(t['status'])}"
    )
    for it in t["iterations"]:
        iter_node = tree.add(
            f"[bold]Iteration {it['iteration']}[/bold] "
            f"({it['wall_s']:.1f}s, ${it['usd']:.4f})"
        )
        plan_node = iter_node.add(f"PLAN -- {len(it['tasks'])} task(s) dispatched")
        for task in it["tasks"]:
            mark = "[green]PASS[/green]" if task["passed"] else "[red]FAIL[/red]"
            detail = f"build={'OK' if task['build_success'] else 'FAILED'}"
            if task["run_verdict"]:
                detail += f" verdict={escape(task['run_verdict'])}"
            if task["module"]:
                detail += f" module={escape(task['module'])}"
            plan_node.add(f"{escape(task['task_id'])} ({escape(task['kind'])}): {mark} -- {detail}")
        if it["failures"]:
            triage_node = iter_node.add(
                f"[yellow]TRIAGE[/yellow] -- {len(it['failures'])} failure(s) diagnosed"
            )
            for f in it["failures"]:
                rec = "[green]recovered[/green]" if f["recovered"] else "[red]not recovered[/red]"
                fix_str = f" fix={escape(repr(f['fix']))}" if f["fix"] else ""
                triage_node.add(
                    f"{escape(f['task_id'])}: diagnosis={escape(repr(f['diagnosis']))}"
                    f"{fix_str} -- {rec}"
                )
    console.print(tree)


@app.command()
def results(
    db_path: str = typer.Option("runs/mace_cli.db", help="Metrics database path"),
    run_id: str = typer.Option(
        None, help="Show this run's failure taxonomy instead of the cross-run table"
    ),
    trace: bool = typer.Option(
        False, "--trace", help="With --run-id, show the full plan/dispatch/triage story "
        "instead of the failure taxonomy"
    ),
) -> None:
    """Read-only report over the metrics database. Needs no shell session --
    just mace.metrics.all_runs()/failure_taxonomy()/trace_run() formatted, so a
    demo doesn't need a live shell to show what past runs did."""
    # width=100: same fix as MaceShell's own Console -- terminal-size
    # auto-detection is unreliable off a real tty (piped/captured output)
    # and produced genuinely corrupted table/tree rendering under it.
    console = Console(width=100)
    # open_db would create a missing file, so a mistyped path would read as
    # "no runs recorded" instead of an error.
    if not os.path.isfile(db_path):
        console.print(f"[bold red]✗ ERROR: no metrics database at {escape(db_path)}[/bold red]")
        raise typer.Exit(code=1)
    db = open_db(os.path.abspath(db_path), ray_placement=False)

    if trace and run_id is None:
        console.print("[bold red]✗ ERROR: --trace needs --run-id[/bold red]")
        raise typer.Exit(code=1)

    if run_id is not None and trace:
        t = trace_run(db, run_id)
        if t is None:
            console.print(f"No recorded run with run_id={escape(repr(run_id))}.")
            return
        _render_trace(console, t)
        return

    if run_id is not None:
        rows = failure_taxonomy(db, run_id)
        if not rows:
            console.print(f"No recorded failures for run_id={escape(repr(run_id))}.")
            return
        table = Table(title=f"Failure taxonomy -- run_id={escape(run_id)}")
        table.add_column("diagnosis")
        table.add_column("total", justify="right")
        table.add_column("recovered", justify="right")
        for row in rows:
            table.add_row(escape(row["diagnosis"]), str(row["total"]), str(row["recovered"]))
        console.print(table)
        return

    runs = all_runs(db)
    if not runs:
        console.print(f"No runs recorded in {escape(db_path)}.")
        return
    table = Table(title=f"Runs -- {escape(db_path)}")
    for col in ("run_id", "core", "mesh", "status", "tasks", "iterations", "wall_s", "usd"):
        table.add_column(col)
    for r in runs:
        table.add_row(
            r["run_id"],
            r["core"],
            f"{r['x_tiles']}x{r['y_tiles']}",
            r["status"],
            str(r["successful_tasks"]),
            str(r["iterations"]),
            f"{r['execution_time_s']:.1f}",
            f"{r['compute_usd']:.4f}",
        )
    console.print(table)

    passed = sum(1 for r in runs if r["status"] == "passed")
    total_cost = sum(r["compute_usd"] for r in runs)
    total_wall = sum(r["execution_time_s"] for r in runs)
    console.print(
        f"\n{passed}/{len(runs)} runs passed -- {total_wall:.1f}s total execution "
        f"time, ${total_cost:.4f} total compute cost."
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
