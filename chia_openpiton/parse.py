"""chia_openpiton.parse — pure parsers for OpenPiton's logs and tool output.

Every function here takes text and returns data: no I/O, no subprocesses, no
Ray. That is deliberate -- these are the functions that decide whether a
simulation passed, so they must be testable against captured logs without a
toolchain present.

Where the patterns come from (OpenPiton source, not guesswork):

* ``piton/verif/env/manycore/pc_cmp.v.pyv`` prints the PASS line when every
  hart in ``finish_mask`` has hit the good trap.
* ``piton/verif/env/manycore/monitor.v.pyv`` owns the single ``fail`` task that
  every monitor funnels into, and the max-cycles abort.
* ``piton/tools/perlmod/Regreport/5.01/.../Regreport.pm`` renders ``status.log``
  (via the ``-post_process_cmd="regreport -1 > status.log"`` hook in
  ``manycore.config``) and the regression summary table.

Note the inconsistent spacing in the RTL: PASS prints ``"%0d: Simulation ..."``
while FAIL prints ``"%0d : Simulation ..."`` (extra space before the colon).
The regexes below tolerate both rather than depending on it.
"""

from __future__ import annotations

import re

from chia_openpiton.state_def import DiagEntry, Verdict

# --- simulation transcript (sim.log / stdout) -------------------------------

_PASS_RE = re.compile(r"Simulation\s*->\s*PASS\b")
_FAIL_RE = re.compile(r"Simulation\s*->\s*FAIL\s*\((.*?)\)")
_MAXCYC_RE = re.compile(r"Simulation\s*->\s*\(terminated by reaching max cycles\s*=\s*(\d+)\)")
_TIMEOUT_TEXT_RE = re.compile(r"TIMEOUT", re.IGNORECASE)

# Observed on a real -rtl_timeout expiry: pc_cmp prints this Info line per
# thread and the run ends WITHOUT ever reaching the monitor's FAIL(TIMEOUT)
# (regreport then reports "Unknown (No Status)"). Used only as a fallback when
# no real verdict line exists, so it can never override a PASS or a FAIL.
_TIMEOUT_HAPPEN_RE = re.compile(r"->\s*timeout happen")

# The verdict line is prefixed with $time, e.g.
#   "179911750: Simulation -> PASS (HIT GOOD TRAP)"
_VERDICT_TIME_RE = re.compile(
    r"^\s*(\d+)\s*:?\s*Simulation\s*->", re.MULTILINE
)


def sim_verdict(text: str) -> Verdict | None:
    """Classify a simulation transcript.

    Returns ``"pass"``, ``"fail"``, ``"timeout"``, ``"maxcycles"``, or ``None``
    when the transcript carries no verdict at all (build never ran, simulator
    died early, log truncated).

    Fail-closed: if any failure marker is present the result is never
    ``"pass"``, even when a PASS line also appears. The testbench calls
    ``$finish`` on the first verdict so this should not happen, but a
    verification gate must not be talked out of a failure by a stray line.
    """
    if not text:
        return None
    fail = _FAIL_RE.search(text)
    if fail is not None:
        return "timeout" if _TIMEOUT_TEXT_RE.search(fail.group(1)) else "fail"
    if _MAXCYC_RE.search(text):
        return "maxcycles"
    if _PASS_RE.search(text):
        return "pass"
    if _TIMEOUT_HAPPEN_RE.search(text):
        return "timeout"
    return None


def sim_time(text: str) -> int | None:
    """Simulation time stamped on the verdict line, or None.

    This is where a run's duration actually comes from: the ``status.log``
    regreport writes for these configurations carries no ``Cyc=`` field, but
    the testbench prefixes its verdict with ``$time``.
    """
    m = _VERDICT_TIME_RE.search(text or "")
    return int(m.group(1)) if m else None


def fail_reason(text: str) -> str:
    """The message inside ``Simulation -> FAIL(...)``, or "" if not present."""
    m = _FAIL_RE.search(text or "")
    return m.group(1).strip() if m else ""


def max_cycles(text: str) -> int | None:
    """The cycle count from a max-cycles abort, or None."""
    m = _MAXCYC_RE.search(text or "")
    return int(m.group(1)) if m else None


# --- status.log (regreport -1) ----------------------------------------------

_DIAG_RE = re.compile(r"^Diag:\s+(\S+)\s+(.+?)\s*$", re.MULTILINE)
_CYC_RE = re.compile(r"\bCyc=\s*(\d+)")
_EXECCYC_RE = re.compile(r"\bExecCyc=\s*(\d+)")
_NUMTILES_RE = re.compile(r"\bNumTiles=\s*(\d+)")


def status_diag(text: str) -> tuple[str, str] | None:
    """``(diag_name, status_text)`` from a ``status.log``, or None.

    The status text is regreport's vocabulary, not ours: ``PASS``, ``FAIL``,
    ``Timeout``, ``MaxCycles Hit``, ``FAIL (Monitor)``, ``Unknown``, ...
    """
    m = _DIAG_RE.search(text or "")
    return (m.group(1), m.group(2).strip()) if m else None


def cycles(text: str) -> int | None:
    """Total simulated cycles (``Cyc=``) from a ``status.log``."""
    m = _CYC_RE.search(text or "")
    return int(m.group(1)) if m else None


def exec_cycles(text: str) -> int | None:
    """Executed cycles (``ExecCyc=``) from a ``status.log``."""
    m = _EXECCYC_RE.search(text or "")
    return int(m.group(1)) if m else None


def num_tiles(text: str) -> int | None:
    """Tile count (``NumTiles=``) reported in a ``status.log``."""
    m = _NUMTILES_RE.search(text or "")
    return int(m.group(1)) if m else None


# --- regreption summary (regreport <dir> -summary) --------------------------

_REGRESS_VERDICT_RE = re.compile(r"^REGRESSION (PASSED|FAILED)\s*$", re.MULTILINE)
_DIAG_COUNT_RE = re.compile(r"^\s*Diag Count:\s*(\d+)", re.MULTILINE)

# regreport's status vocabulary, as it appears in the summary column.
_SUMMARY_ROWS = (
    "PASS",
    "FAIL",
    "Diag Problem",
    "License Problem",
    "MaxCycles Hit",
    "Socket Problem",
    "Timeout",
    "LessThreads",
    "Simics Problem",
    "Performance",
    "Killed By Job Q",
    "Unknown",
    "UnFinished",
    "flexlm error",
)


def regress_summary(text: str) -> dict[str, object]:
    """Parse a ``regreport ... -summary`` table.

    Returns ``{"passed": bool|None, "counts": {status: n}, "diag_count": int|None}``.
    ``passed`` is None when the summary carries no REGRESSION verdict line.
    """
    text = text or ""
    counts: dict[str, int] = {}
    for label in _SUMMARY_ROWS:
        m = re.search(rf"^\s*{re.escape(label)}:\s*(\d+)", text, re.MULTILINE)
        if m:
            counts[label] = int(m.group(1))
    verdict = _REGRESS_VERDICT_RE.search(text)
    count = _DIAG_COUNT_RE.search(text)
    return {
        "passed": (verdict.group(1) == "PASSED") if verdict else None,
        "counts": counts,
        "diag_count": int(count.group(1)) if count else None,
    }


# --- sims itself ------------------------------------------------------------

_DIE_RE = re.compile(r"DIE\.\s*(.+?)(?:\s+at\s+\S+\s+line\s+\d+\.?)?\s*$", re.MULTILINE)
_MODEL_DIR_RE = re.compile(r"^sims:\s*creating model directory\s+(.+?)\s*$", re.MULTILINE)


def sims_die(text: str) -> str:
    """The message from ``sims: Caught a SIGDIE. <message> at <file> line N.``

    Returns "" when sims did not die. The Perl file/line suffix is stripped so
    the message is stable across OpenPiton revisions.
    """
    m = _DIE_RE.search(text or "")
    return m.group(1).strip() if m else ""


def model_dir(text: str) -> str:
    """The model directory sims reported creating, or ""."""
    m = _MODEL_DIR_RE.search(text or "")
    return m.group(1).strip() if m else ""


# --- build failures ---------------------------------------------------------

# Ordered most-specific first: the first match is reported, so a Verilator
# diagnostic beats the generic "%Error" summary line that follows it.
_BUILD_FAILURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "verilator_needs_timing_flag",
        re.compile(r"%Error-NEEDTIMINGOPT"),
    ),
    (
        "verilator_bad_option",
        re.compile(r"%Error:\s*Invalid option:\s*(\S+)"),
    ),
    (
        "pch_link_failure",
        re.compile(r"undefined reference to [`'\"]main"),
    ),
    (
        "verilog_error",
        re.compile(r"%Error(?:-[A-Z]+)?:"),
    ),
    (
        "make_failed",
        re.compile(r"^make(?:\[\d+\])?:\s*\*\*\*", re.MULTILINE),
    ),
    (
        "compile_error",
        re.compile(r"^\s*\S+:\d+:\d+:\s*error:", re.MULTILINE),
    ),
)


def build_failure_reason(stdout: str, stderr: str = "") -> str:
    """A short, stable tag for why a build failed, or "".

    Tags (not free text) so the loop's failure taxonomy can count them:
    ``verilator_needs_timing_flag``, ``verilator_bad_option``,
    ``pch_link_failure``, ``verilog_error``, ``make_failed``, ``compile_error``,
    or a ``sims_die:<message>`` fallback.
    """
    blob = f"{stdout or ''}\n{stderr or ''}"
    for tag, pattern in _BUILD_FAILURES:
        if pattern.search(blob):
            return tag
    die = sims_die(blob)
    return f"sims_die:{die}" if die else ""


# --- verilator ---------------------------------------------------------------

_VERILATOR_RE = re.compile(r"Verilator\s+(\d+)\.(\d+)")


def verilator_version(text: str) -> tuple[int, int] | None:
    """``(major, minor)`` from ``verilator --version`` output, or None.

    Handles both release strings (``Verilator 4.038 2020-07-11 rev ...``) and
    development builds (``Verilator 5.049 devel rev vUNKNOWN-built...``).
    """
    m = _VERILATOR_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def needs_no_timing(version_text: str) -> bool:
    """Whether this Verilator requires an explicit ``--timing``/``--no-timing``.

    Verilator 5 refuses OpenPiton's bare ``#1`` delays in the testbench monitors
    unless told how to handle timing; Verilator 4 has no such flag at all and
    errors if given one. OpenPiton pins 4.014, but the CHIA worker image ships
    apt's 4.038 and this dev machine has a 5.x nightly, so the flag is decided
    per worker at runtime rather than assumed.
    """
    version = verilator_version(version_text)
    return version is not None and version[0] >= 5


# --- diaglist -----------------------------------------------------------------

_COMMENT_RE = re.compile(r"//.*$")
_RUNARGS_OPEN_RE = re.compile(r"^<runargs(?:\s+(.*))?>$")


def diaglist_group(text: str, group: str) -> tuple[DiagEntry, ...]:
    """Tests in one ``<group>...</group>`` of a ``master_diaglist_*`` file.

    This format is XML-shaped but not XML: tags are matched by name alone
    (attributes on a group's own opening tag, like the ``sys=manycore``
    build tags, are ignored), ``//`` starts a comment that runs to end of
    line, and groups nest arbitrarily (``tile1_mini`` contains
    ``princeton-test`` and ``tile1_mini_icache`` as sub-groups plus loose
    test lines of its own). Selecting a group returns every test found
    anywhere in its span, sub-groups included -- any nested tag this
    function does not otherwise recognise is simply skipped over.

    A ``<runargs ...>`` block's own flags apply to every test line inside
    it. A test line is ``alias source [args...]``; args may appear on
    either side of source (OpenPiton's own file has both), so the source is
    identified as the first token after the alias that does not start with
    ``-``, and every other token becomes a trailing arg, in file order,
    appended after the enclosing runargs' flags.

    Args:
        text: Full contents of a master_diaglist-style file.
        group: Group name to select, e.g. ``"ariane_tile1_simple"`` -- what
            ``-group=`` would select.

    Returns:
        The group's tests in file order. Empty if the group has no tests.

    Raises:
        ValueError: If no ``<group>...</group>`` block exists in ``text``.
    """
    lines = [_COMMENT_RE.sub("", ln).strip() for ln in text.splitlines()]

    open_re = re.compile(rf"^<{re.escape(group)}(?:\s[^>]*)?>$")
    close_re = re.compile(rf"^</{re.escape(group)}>$")
    start = next((i for i, ln in enumerate(lines) if open_re.match(ln)), None)
    if start is None:
        raise ValueError(f"group {group!r} not found in diaglist")
    end = next(i for i in range(start + 1, len(lines)) if close_re.match(lines[i]))

    entries: list[DiagEntry] = []
    runargs: tuple[str, ...] = ()
    for ln in lines[start + 1 : end]:
        if not ln:
            continue
        runargs_open = _RUNARGS_OPEN_RE.match(ln)
        if runargs_open:
            runargs = tuple((runargs_open.group(1) or "").split())
            continue
        if ln == "</runargs>":
            runargs = ()
            continue
        if ln.startswith("<"):
            continue  # some other tag (cmp_default, a nested sub-group, ...)
        parts = ln.split()
        alias, rest = parts[0], parts[1:]
        source_idx = next((i for i, tok in enumerate(rest) if not tok.startswith("-")), None)
        if source_idx is None:
            continue  # no source file on this line -- not a test we can run
        source = rest[source_idx]
        extra = tuple(tok for i, tok in enumerate(rest) if i != source_idx)
        entries.append(DiagEntry(alias=alias, source=source, args=runargs + extra))
    return tuple(entries)
