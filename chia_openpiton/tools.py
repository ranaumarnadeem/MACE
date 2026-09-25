"""chia_openpiton.tools — the agent-facing MCP interface over OpenPitonWorkspaceNode.

This is what closes the gap between "Python can drive OpenPiton" and "an agent
can drive OpenPiton": every method here is a thin adapter over
:class:`~chia_openpiton.openpiton_workspace.OpenPitonWorkspaceNode`, calling its
``@ChiaFunction`` members **locally** (in-process, no extra Ray hop) so the
agentic path and the programmatic path share one implementation — matching
:class:`chia.simulators.gem5.Gem5ToolServer`'s convention.

Operational state (the checkout root, timeouts, where gate workloads live) is
bound at construction and never exposed to the model: the agent only chooses
*what* to run, never *where* or *how* the environment is wired.

Build and run go through :class:`~chia.base.tools.AsyncJobTool.AsyncJobTool`
rather than a plain blocking call. A build that takes minutes would otherwise
hold the MCP transport open with no traffic for the whole job -- exactly the
failure `AsyncJobTool` was built to avoid, and exactly the shape of a Verilator
build or an RTL simulation. Build and run share one job slot (only one can run
at a time, which matches the natural build-then-run order anyway); poll with
one shared ``{name}_job_status``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from chia.base.tools.AsyncJobTool import AsyncJobTool

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode, _require_root
from chia_openpiton.parse import first_divergence
from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig, PitonRunResult

logger = logging.getLogger(__name__)

# Which log a grep/collect call reads, and its filename under a run directory.
_LOG_SOURCES: dict[str, str] = {
    "sim_log": "sim.log",
    "status_log": "status.log",
    "fake_uart": "fake_uart.log",
}

# compare_to_fixture()'s allowlist -- a fixture_name must resolve to a file
# directly inside this directory, never an arbitrary path the model names.
_FIXTURES_DIR = Path(__file__).resolve().parent / "test" / "fixtures"

# Limits on the regex an LLM passes to grep. Python's re has no timeout, and
# a group that repeats something already repeating, as in (a+)+, can
# backtrack for minutes on one long line, so such patterns are refused and
# each line is searched only up to _GREP_MAX_LINE characters.
_GREP_MAX_PATTERN = 256
_GREP_MAX_LINE = 4000
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*(?:[*+]|\{\d*,\d*\})(?:[^()\\]|\\.)*\)(?:[*+]|\{\d*,)")


def _grep_lines(text: str, pattern: str, context: int, max_lines: int) -> str:
    """Regex-search *text* line by line, returning matches with *context* lines
    of padding, capped at *max_lines*. Never raises on a bad pattern."""
    if len(pattern) > _GREP_MAX_PATTERN:
        return f"ERROR: pattern is {len(pattern)} characters; keep it under {_GREP_MAX_PATTERN}"
    if _NESTED_QUANTIFIER.search(pattern):
        return (
            f"ERROR: pattern {pattern!r} repeats a group that itself repeats, "
            f"which can run for minutes; use a simpler pattern"
        )
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad regex {pattern!r}: {e}"
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if rx.search(line[:_GREP_MAX_LINE])]
    if not hits:
        return f"(no lines match {pattern!r})"
    shown: set[int] = set()
    for i in hits:
        shown.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    out = [lines[j] for j in sorted(shown)[:max_lines]]
    return "\n".join(out)


def _render_config(config: PitonConfig) -> str:
    return (
        f"core={config.core} mesh={config.x_tiles}x{config.y_tiles} "
        f"network={config.network_config}\n"
        f"config_rtl={list(config.config_rtl)}\n"
        f"caches={config.caches}\n"
        f"extra_flags={list(config.extra_flags)}\n"
        f"build_id={config.build_id}"
    )


def _job_error(job_type: str, exc: Exception) -> dict:
    """Result for a build or run job whose node call raised.

    AsyncJobTool stores no result when its job raises, so job_status would
    report the job as running forever.
    """
    logger.error("%s job raised", job_type, exc_info=exc)
    return {"job_type": job_type, "success": False, "error": f"{type(exc).__name__}: {exc}"}


class PitonToolServer(AsyncJobTool):
    """LLM-facing MCP tool over one OpenPiton checkout.

    Exposes: ``{name}_build``, ``{name}_run``, ``{name}_job_status``,
    ``{name}_grep``, ``{name}_collect``, ``{name}_config_get``,
    ``{name}_config_set``, ``{name}_compare_to_fixture``,
    ``{name}_symbol_check``. Pass ``expose=(...)`` to register only a subset
    -- e.g. ``expose=("config_get", "config_set")`` for an agent that only
    edits configuration and hands off building to something else, or
    ``expose=("grep", "collect", "compare_to_fixture", "symbol_check")`` for
    a read-only diagnostic agent that inspects an already-completed failure
    rather than driving a new build/run itself (pass that failure's build/
    run as ``last_build``/``last_run``).

    What the model sees is fixed at construction: ``ChiaTool.__post_init__``
    ships a pickled snapshot of this object to the Ray actor that answers
    every MCP call, so assigning to this (caller-side) object afterwards
    never reaches it. That is why an already-completed build/run is a
    constructor argument -- to inspect a different one, start a new server.

    Co-location: point ``task_options`` at the same bundle as whatever
    workspace node owns this checkout (``node.task_options``), so the tool's
    builds and runs land on the worker that holds the checkout.
    """

    def __init__(
        self,
        name: str,
        piton_root: str,
        config: PitonConfig,
        *,
        asm_diag_root: str | None = None,
        build_timeout_s: int = 7200,
        run_timeout_s: int = 3600,
        expose: tuple[str, ...] | None = None,
        task_options: dict | None = None,
        last_build: PitonBuildArtifact | None = None,
        last_run: PitonRunResult | None = None,
    ):
        super().__init__(name, task_options=task_options)
        self.piton_root = _require_root(piton_root)
        self.asm_diag_root = asm_diag_root
        self.build_timeout_s = build_timeout_s
        self.run_timeout_s = run_timeout_s

        self._config = config
        # Must be set before super().__post_init__() below snapshots this
        # object for the server actor -- see the class docstring.
        self._last_build: PitonBuildArtifact | None = last_build
        self._last_run: PitonRunResult | None = last_run

        registry = {
            "build": self.build,
            "run": self.run,
            "job_status": self.job_status,
            "grep": self.grep,
            "collect": self.collect,
            "config_get": self.config_get,
            "config_set": self.config_set,
            "compare_to_fixture": self.compare_to_fixture,
            "symbol_check": self.symbol_check,
        }
        selected = tuple(registry) if expose is None else tuple(expose)
        unknown = [t for t in selected if t not in registry]
        if unknown:
            raise ValueError(
                f"PitonToolServer expose={expose!r}: unknown tool(s) {unknown}; "
                f"valid options are {sorted(registry)}"
            )
        for t in selected:
            self.mcp.add_tool(registry[t], name=f"{name}_{t}")
        super().__post_init__()

    # -- long-running: build / run / poll -------------------------------------

    def build(self, clean: bool = False) -> dict:
        """Start a Verilator build for the current configuration.

        Long-running (measured: ~37s for a 1x1 Ariane tile; larger meshes take
        longer) -- returns immediately. Poll with ``{name}_job_status``. Use
        ``{name}_config_set`` first to change the mesh, core, or RTL defines.

        Calling this again for a configuration that already built successfully
        is cheap while the checkout's files are unchanged: it is served from
        disk instead of re-invoking the toolchain (``job_status``'s ``reused``
        field says whether that happened). An edit to the checkout makes it
        rebuild. Pass ``clean=True`` to force a rebuild.

        Args:
            clean: Force a rebuild even if this configuration already built
                successfully; also discards the previous build's output.
        """
        config = self._config  # snapshot now: config_set must not race the build

        def _work() -> dict:
            try:
                art = OpenPitonWorkspaceNode.build(
                    self.piton_root, config, clean=clean, timeout_seconds=self.build_timeout_s
                )
            except Exception as e:
                return _job_error("build", e)
            self._last_build = art
            return {
                "job_type": "build",
                "success": art.success,
                "returncode": art.returncode,
                "failure_reason": art.failure_reason,
                "reused": art.reused,
                "wall_time_s": art.wall_time_s,
                "binary_path": art.binary_path,
            }

        return self._job_start(_work)

    def run(
        self,
        test: str,
        precompiled: bool = False,
        finish_mask: str | None = None,
        rtl_timeout: int | None = None,
        max_cycle: int | None = None,
    ) -> dict:
        """Start a simulation run of *test* against the current built model.

        Requires a successful ``{name}_build`` first. Long-running -- poll with
        ``{name}_job_status``. Success is judged from the testbench transcript,
        never the exit code: an RTL simulation exits 0 whether or not the
        program passed.

        Args:
            test: Diag name, e.g. ``"hello_world.c"`` or ``"rv64ui-p-addi.S"``.
            precompiled: Use a prebuilt riscv-tests ELF instead of compiling.
            finish_mask: ``+finish_mask`` override; defaults to one digit per
                tile (every hart must trap good for a multi-tile run to pass).
            rtl_timeout: ``+TIMEOUT`` in cycles.
            max_cycle: ``+max_cycle`` abort threshold.
        """
        config = self._config

        def _work() -> dict:
            try:
                res = OpenPitonWorkspaceNode.run(
                    self.piton_root,
                    config,
                    test,
                    precompiled=precompiled,
                    asm_diag_root=self.asm_diag_root,
                    finish_mask=finish_mask,
                    rtl_timeout=rtl_timeout,
                    max_cycle=max_cycle,
                    timeout_seconds=self.run_timeout_s,
                )
            except Exception as e:
                return _job_error("run", e)
            self._last_run = res
            return {
                "job_type": "run",
                "success": res.success,
                "returncode": res.returncode,
                "verdict": res.verdict,
                "sim_time": res.sim_time,
                "wall_time_s": res.wall_time_s,
            }

        return self._job_start(_work)

    def job_status(self, wait_seconds: int = 30) -> dict:
        """Poll the most recently started build or run job.

        Blocks up to *wait_seconds* (capped at 120s so the call always
        returns) for it to finish. ``done=False`` means keep polling; the
        result's ``job_type`` says whether a build or a run just finished.
        If the job raised an exception, ``success`` is False and ``error``
        gives the exception's type and message.
        """
        return self._job_status(wait_seconds)

    # -- fast: inspect the last run --------------------------------------------

    def grep(self, source: str, pattern: str, context: int = 3, max_lines: int = 40) -> str:
        """Search the most recent run's logs for a regex.

        Args:
            source: Which log: ``"sim_log"`` (the simulator transcript --
                where PASS/FAIL/timeout appear), ``"status_log"``, or
                ``"fake_uart"`` (what the program printed to its console).
            pattern: A Python regex.
            context: Lines of context shown around each match.
            max_lines: Cap on lines returned.
        """
        if self._last_run is None:
            return f"ERROR: no run yet; call {self.name}_run(...) first"
        if source not in _LOG_SOURCES:
            return f"ERROR: source must be one of {sorted(_LOG_SOURCES)}, got {source!r}"
        path = os.path.join(self._last_run.run_dir, _LOG_SOURCES[source])
        if not os.path.isfile(path):
            return f"(no {_LOG_SOURCES[source]} in this run)"
        with open(path, errors="replace") as f:
            text = f.read()
        return _grep_lines(text, pattern, context, max_lines)

    def collect(self, pattern: str = "*", max_bytes: int = 200_000) -> str:
        """Fetch small text files from the most recent run's directory.

        Args:
            pattern: Glob relative to the run directory (``**`` is recursive).
            max_bytes: Per-file cap; larger files are listed, not returned.
        """
        if self._last_run is None:
            return f"ERROR: no run yet; call {self.name}_run(...) first"
        result = OpenPitonWorkspaceNode.collect(
            self.piton_root, self._last_run.run_dir, (pattern,), max_bytes_per_file=max_bytes
        )
        if not result.files and not result.skipped:
            return f"(no files under {result.base_dir} match {pattern!r})"
        lines = [f"{len(result.files)} file(s):"]
        for name, content in result.files.items():
            lines.append(f"--- {name} ---")
            lines.append(content)
        for name, size in result.skipped.items():
            lines.append(f"(skipped {name}: {size} bytes, over the {max_bytes}-byte cap)")
        return "\n".join(lines)

    # -- diagnosis: the manual fixture-diff/objdump technique, automated --------
    # See docs/TECHNICAL_GUIDE.md's PicoRV32 and 2x2-mesh findings: a hang was
    # only trusted as a real RTL gap (not a bad build) after diffing the run's
    # sim.log against a known-good transcript and cross-checking the compiled
    # binary's own objdump output against the run's symbol.tbl. These two
    # tools hand a model the same raw data a human read by hand.

    def compare_to_fixture(self, fixture_name: str, max_context: int = 5) -> str:
        """Diff the current run's sim.log against a known-good reference
        transcript, line for line, reporting the exact point of first
        divergence.

        Args:
            fixture_name: A captured reference transcript's filename under
                chia_openpiton/test/fixtures/ (e.g. ``"run_pass_sim.log"``).
                Restricted to that directory's own files -- not an
                arbitrary path.
            max_context: Lines of context shown around the divergence
                point, from both the fixture and this run.
        """
        if self._last_run is None:
            return f"ERROR: no run yet; call {self.name}_run(...) first"
        fixture_path = (_FIXTURES_DIR / fixture_name).resolve()
        if fixture_path.parent != _FIXTURES_DIR.resolve() or not fixture_path.is_file():
            available = sorted(p.name for p in _FIXTURES_DIR.glob("*.log"))
            return f"ERROR: unknown fixture {fixture_name!r}; available: {available}"
        sim_log_path = os.path.join(self._last_run.run_dir, "sim.log")
        if not os.path.isfile(sim_log_path):
            return "(no sim.log in this run)"
        reference = fixture_path.read_text(errors="replace")
        with open(sim_log_path, errors="replace") as f:
            actual = f.read()
        result = first_divergence(reference, actual)
        if result is None:
            shorter = min(len(reference.splitlines()), len(actual.splitlines()))
            return f"identical to {fixture_name} through all {shorter} shared line(s)"
        line_no, _ref_line, _act_line = result
        ref_lines, act_lines = reference.splitlines(), actual.splitlines()
        start = max(0, line_no - 1 - max_context)
        ref_ctx = "\n".join(ref_lines[start:line_no])
        act_ctx = "\n".join(act_lines[start:line_no])
        return (
            f"diverges from {fixture_name} at line {line_no}:\n"
            f"--- {fixture_name} (lines {start + 1}-{line_no}) ---\n{ref_ctx}\n"
            f"--- this run's sim.log (lines {start + 1}-{line_no}) ---\n{act_ctx}"
        )

    def symbol_check(self) -> str:
        """Real ``objdump -f``/``-t`` output for the current run's compiled
        diag binary, plus the run's own ``symbol.tbl``, side by side.

        Deliberately returns raw data rather than a precomputed match/
        mismatch verdict: ``good_trap``/``bad_trap`` in symbol.tbl are not
        named symbols inside the binary itself (confirmed against a real
        build -- the address symbol.tbl calls ``good_trap`` is the same
        address objdump's own symbol table names ``pass``), so correlating
        them is a real reasoning step, not a lookup this tool can do for
        the model.
        """
        if self._last_run is None:
            return f"ERROR: no run yet; call {self.name}_run(...) first"
        run_dir = self._last_run.run_dir
        binary = os.path.join(run_dir, "diag.exe")
        symtbl = os.path.join(run_dir, "symbol.tbl")
        if not os.path.isfile(binary):
            return "(no diag.exe in this run directory)"
        try:
            entry = subprocess.run(["objdump", "-f", binary], capture_output=True, text=True, timeout=30)
            symbols = subprocess.run(["objdump", "-t", binary], capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return "ERROR: 'objdump' was not found on PATH"
        if entry.returncode != 0:
            return f"ERROR: 'objdump -f {binary}' failed (exit {entry.returncode}): {entry.stderr}"
        if symbols.returncode != 0:
            return f"ERROR: 'objdump -t {binary}' failed (exit {symbols.returncode}): {symbols.stderr}"
        symtbl_text = "(not found in this run directory)"
        if os.path.isfile(symtbl):
            with open(symtbl, errors="replace") as f:
                symtbl_text = f.read()
        return (
            f"--- objdump -f diag.exe ---\n{entry.stdout}"
            f"--- objdump -t diag.exe (symbol table) ---\n{symbols.stdout}"
            f"--- this run's own symbol.tbl ---\n{symtbl_text}"
        )

    # -- configuration ----------------------------------------------------------

    def config_get(self) -> str:
        """Show the current configuration: mesh, core, RTL defines, caches."""
        return _render_config(self._config)

    def config_set(
        self,
        x_tiles: int | None = None,
        y_tiles: int | None = None,
        core: str | None = None,
        network_config: str | None = None,
        config_rtl: list[str] | None = None,
        extra_flags: list[str] | None = None,
    ) -> str:
        """Change the configuration for the next ``{name}_build``.

        Only given fields change; the rest keep their current value. This
        re-resolves the config against the checkout (source revision,
        Verilator version), so the next build gets a fresh ``-build_id`` and
        won't be confused with any earlier one.
        """
        c = self._config
        new = OpenPitonWorkspaceNode.configure(
            self.piton_root,
            x_tiles=x_tiles if x_tiles is not None else c.x_tiles,
            y_tiles=y_tiles if y_tiles is not None else c.y_tiles,
            core=core if core is not None else c.core,
            network_config=network_config if network_config is not None else c.network_config,
            config_rtl=tuple(config_rtl) if config_rtl is not None else c.config_rtl,
            caches=c.caches,
            extra_flags=tuple(extra_flags) if extra_flags is not None else c.extra_flags,
        )
        self._config = new
        return "OK, config updated:\n" + _render_config(new)
