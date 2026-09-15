"""mace.cli -- the interactive command shell (init, read_verilog, top_module,
read_spec, set_core, run, write_report), and its scriptable Typer entry point.

See mace/cli/shell.py for the shell itself; this file only wires the package
together (the `mace` console-script entry point pyproject.toml points at).
"""

from __future__ import annotations

from mace.cli.shell import app, main

__all__ = ["app", "main"]
