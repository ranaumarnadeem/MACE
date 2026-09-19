"""Layer 2 of the unit-testing gap (see plan §21): scaffold a new OpenPiton
unit-test environment for a target module via its own create_env.py, and
extract the module's real port list so an agent can reconcile a scaffolded
testbench's dummy DUT connections against it.

This module deliberately does not attempt a full Verilog parser. create_env.py
already produces a generic template (dummy port names like ``.input0``,
``.output0``) that a human is expected to hand-edit -- the project owner's own
requirement is that the MACE loop/agent do that hand-edit instead, since
testbenches can't be hardcoded. The functions here hand the agent the raw
material for that (the real DUT's port-list text and best-effort port names),
not a finished reconciliation -- the actual signal-name/width matching is
exactly the "small edit" judgment call the agent is meant to make.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_PREPROC_LINE = re.compile(r"^\s*`(ifdef|ifndef|elsif|else|endif)\b")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//.*")
_PORT_KEYWORDS = {"input", "output", "inout", "reg", "wire", "wand", "wor", "signed", "unsigned", "logic"}


class ModuleNotFoundError_(ValueError):
    """Raised when the target module isn't declared in the given source."""


def extract_module_port_list(verilog_text: str, module_name: str) -> str:
    """The raw text between a module's port-list parens, e.g. everything
    between ``module foo #(...) (`` and the matching ``);``.

    Handles an optional ``#( ... )`` parameter block before the port list by
    scanning past it first. Uses a paren-depth scan rather than a single
    regex since real port lists (see picorv32.v) contain nested brackets and
    `` `MACRO``-based widths that a naive non-greedy regex would mis-match on.
    """
    header = re.search(rf"\bmodule\s+{re.escape(module_name)}\b", verilog_text)
    if not header:
        raise ModuleNotFoundError_(f"module {module_name!r} not found")

    pos = header.end()
    # Skip an optional #( parameter, list ) block before the port list.
    stripped = verilog_text[pos:].lstrip()
    skip = len(verilog_text[pos:]) - len(stripped)
    if stripped.startswith("#("):
        depth = 0
        i = pos + skip + 1  # position of the '('
        start = i
        while i < len(verilog_text):
            if verilog_text[i] == "(":
                depth += 1
            elif verilog_text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        pos = i + 1
    else:
        pos = pos + skip

    open_paren = verilog_text.index("(", pos)
    depth = 0
    i = open_paren
    while i < len(verilog_text):
        if verilog_text[i] == "(":
            depth += 1
        elif verilog_text[i] == ")":
            depth -= 1
            if depth == 0:
                return verilog_text[open_paren + 1 : i]
        i += 1
    raise ModuleNotFoundError_(f"unbalanced parens scanning module {module_name!r}'s port list")


def parse_port_names(port_list_text: str) -> list[str]:
    """Best-effort real port names from a raw port-list text (see
    :func:`extract_module_port_list`), in declaration order, duplicates
    removed. Not a full Verilog parser -- strips comments and standalone
    preprocessor-conditional lines, then reads each comma-separated
    declaration segment as ``[direction] [type] [width] name`` and keeps the
    trailing identifier, so multi-name lines like ``input clk, reset_l,``
    yield both names.
    """
    text = _BLOCK_COMMENT.sub("", port_list_text)
    text = _LINE_COMMENT.sub("", text)
    lines = [ln for ln in text.splitlines() if not _PREPROC_LINE.match(ln)]
    text = "\n".join(lines)

    names: list[str] = []
    seen: set[str] = set()
    for segment in text.split(","):
        tokens = [t for t in re.split(r"\s+", segment.strip()) if t]
        tokens = [t for t in tokens if not t.startswith("[")]
        tokens = [t for t in tokens if t not in _PORT_KEYWORDS]
        if not tokens:
            continue
        name = tokens[-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name) and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def read_dut_ports(rtl_path: str, module_name: str) -> list[str]:
    """:func:`parse_port_names` for the module declared in the file at
    ``rtl_path``. Raises :class:`ModuleNotFoundError_` if the module isn't
    found there (wrong file, or the module is generated/included elsewhere).
    """
    text = Path(rtl_path).read_text()
    return parse_port_names(extract_module_port_list(text, module_name))


def scaffold_env(
    piton_root: str, env_name: str, dv_root: str | None = None, module_dv_path: str | None = None
) -> dict:
    """Create a new OpenPiton unit-test environment skeleton via the
    project's own ``create_env.py``, idempotently.

    create_env.py itself aborts if the target already exists, so this checks
    first and no-ops rather than treating a second call as an error --
    matching scripts/patch_openpiton.sh's own idempotent-patch convention.
    Returns the paths of the files create_env.py is documented to produce.

    ``module_dv_path`` (the target module's real RTL path, relative to
    ``$DV_ROOT`` -- e.g. ``design/common/rtl/alarm_counter.v``), when given,
    triggers two real, mechanical fixes to what create_env.py generates by
    default -- found by actually building a scaffolded environment (see
    _fix_config_for_verilator's own docstring for why these are real, not
    guessed), applied only right after a fresh scaffold, never to an
    already-existing one:

    1. create_env.py's generated ``.config`` targets VCS by default (its
       ``-env_base=``/most ``-vcs_build_args=`` lines aren't recognized by
       ``sims``'s Verilator path at all -- a real ``SIGDIE`` on
       ``-env_base=``, confirmed directly, not assumed).
    2. Its placeholder DUT ``-flist=`` line
       (``$DV_ROOT/design/<env>/rtl/Flist.<env>``) never points anywhere
       real. The fix is not to guess the right ``Flist.*`` aggregate for an
       arbitrary module (there often isn't one specific to just it) but to
       drop that line and add the module's own real path to the
       testbench's own flist instead -- confirmed the only working shape:
       ``-flist=`` must name a manifest file (a list of paths), never a
       ``.v`` source file directly, or ``sims``'s own flist processing
       corrupts silently (an "Use of uninitialized value" Perl warning, then
       a Verilator ``Invalid option: +`` from what it's actually handed).
    """
    root = Path(piton_root)
    dv = Path(dv_root) if dv_root else root / "piton"
    env_dir = dv / "verif" / "env" / env_name
    top_v = env_dir / f"{env_name}_top.v"
    flist_path = env_dir / f"{env_name}.flist"
    config_path = dv / "tools" / "src" / "sims" / f"{env_name}.config"

    if env_dir.is_dir():
        return {
            "created": False,
            "env_dir": str(env_dir),
            "top_v": str(top_v),
            "flist": str(flist_path),
            "config": str(config_path),
        }

    create_env_py = dv / "verif" / "env" / "create_env.py"
    result = subprocess.run(
        ["python3", str(create_env_py), f"--name={env_name}"],
        cwd=str(root),
        env={"DV_ROOT": str(dv), **_inherited_path_env()},
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"create_env.py --name={env_name} failed (rc={result.returncode}): "
            f"{result.stderr[-2000:]}"
        )

    if module_dv_path is not None:
        config_path.write_text(_fix_config_for_verilator(config_path.read_text(), env_name))
        with flist_path.open("a") as f:
            f.write(f"$DV_ROOT/{module_dv_path}\n")

    return {
        "created": True,
        "env_dir": str(env_dir),
        "top_v": str(top_v),
        "flist": str(flist_path),
        "config": str(config_path),
        "stdout": result.stdout,
    }


def _fix_config_for_verilator(config_text: str, env_name: str) -> str:
    """create_env.py's generated ``.config``, made buildable under
    ``sims -vlt_build`` -- see :func:`scaffold_env`'s own docstring for how
    these two problems were actually found (a real build attempt, not a
    guess).
    """
    drop_exact = {
        f"-flist=$DV_ROOT/design/{env_name}/rtl/Flist.{env_name}",
        f"-env_base=$DV_ROOT/verif/env/{env_name}",
        "-vcs_build_args=+notimingcheck",
        "-vcs_build_args=+nospecify",
        "-vcs_build_args=+nbaopt",
        "-vcs_build_args=-Xstrict=1 -notice",
    }
    lines = []
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped in drop_exact:
            continue
        if stripped.startswith("-vcs_build_args=+incdir+"):
            line = line.replace("-vcs_build_args=", "-sim_build_args=", 1)
        lines.append(line)
    return "\n".join(lines) + "\n"


def _inherited_path_env() -> dict:
    import os

    return {"PATH": os.environ.get("PATH", "")}


def module_name_from_path(module_path: str) -> str:
    """The bare module name a unit_test task's ``spec`` (an RTL path) is
    expected to declare, e.g. ``picorv32.v`` -> ``picorv32``,
    ``l15_pipeline.v.pyv`` -> ``l15_pipeline`` (this project's ``.v.pyv``
    convention for python-preprocessed Verilog carries two extensions,
    hence ``.split(\".\")[0]`` rather than a single ``Path.stem``).
    """
    return Path(module_path).stem.split(".")[0]


def unit_test_env_name(module_path: str) -> str:
    """The scaffolded env name for a target module, e.g. ``picorv32.v`` ->
    ``picorv32_ut``. Matches this session's own ``pico_reset_ut`` naming by
    convention (``<module>_ut``), not ``pico_reset_ut``'s specific name --
    that one is hand-authored for one behavior, not module-generic.

    Deliberately just ``Path.stem`` (unlike :func:`module_name_from_path`):
    this only needs to be a valid, distinct scaffold directory name, not the
    exact real Verilog module identifier, so a ``.v.pyv`` file's env name
    keeps its middle ``.v`` (``l15_pipeline.v_ut``) rather than being
    collapsed to match the real module name.
    """
    stem = Path(module_path).stem
    return f"{stem}_ut"
