"""mace.tools -- the agent-facing MCP tools that edit files: one for
adapting a scaffolded unit-test testbench (the "small edit" step
mace.loop._run_unit_test_step hands off to an LLM: reconciling a
create_env.py-scaffolded testbench's dummy DUT port connections against a
target module's real ports), and RtlEditTool for an rtl task's changes to
the design under piton/design/.

Deliberately NOT a general BashTool. The project owner's own scoping for
this step was narrow ("edit access to the scaffolded _top.v file", not the
whole checkout), and this project's own standing caution ("BashTool is not
a sandbox" -- plan.md sec 10) argues against handing an agent a raw shell
just to change a handful of port connections. TestbenchEditTool below is
scoped to exactly one file, bound at construction: there is no path
argument on either of its two methods, so there is nothing for an agent to
point elsewhere even if it tried.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from chia.base.tools.ChiaTool import ChiaTool

# The part of a checkout an rtl task may change: the design. The testbench
# and the monitors that judge a run live under piton/verif/, out of reach,
# so a task cannot pass by changing what checks it.
RTL_EDIT_ROOT = "piton/design"
_READ_LINES = 400
_SEARCH_HITS = 60


class TestbenchEditTool(ChiaTool):
    """MCP tool over exactly two files: a scaffolded unit-test testbench
    (read/write) and the real target module's own RTL source (read-only).

    Exposes ``{name}_read_testbench``, ``{name}_write_testbench``, and
    ``{name}_read_dut_source`` -- no other method, no path parameter on any
    of them, so an agent given only this tool can read and rewrite the one
    testbench file and read the one DUT file it was constructed for, and
    nothing else in the checkout. The DUT source is read-only: reconciling
    a testbench against a module is never a reason to edit the module
    itself.

    Read access to the real DUT source (not just a pre-parsed port-name
    list) exists because a real build found real gaps a name-only summary
    can't cover -- exact bit widths, and create_env.py's own template
    placeholders (SRC_BIT_WIDTH and friends) that need real values derived
    from the module, not guessed.
    """

    # Not a pytest test class -- the name just happens to start with "Test"
    # (Testbench...); this tells pytest's collector to leave it alone.
    __test__ = False

    def __init__(
        self, name: str, top_v_path: str, dut_source_path: str, task_options: dict | None = None
    ):
        super().__init__(name, task_options=task_options)
        self.top_v_path = str(Path(top_v_path))
        self.dut_source_path = str(Path(dut_source_path))
        self.mcp.add_tool(self.read_testbench, name=f"{name}_read_testbench")
        self.mcp.add_tool(self.write_testbench, name=f"{name}_write_testbench")
        self.mcp.add_tool(self.read_dut_source, name=f"{name}_read_dut_source")
        super().__post_init__()

    def read_testbench(self) -> str:
        """The scaffolded testbench file's current content."""
        return Path(self.top_v_path).read_text()

    def write_testbench(self, content: str) -> str:
        """Overwrite the scaffolded testbench file with *content*.

        Args:
            content: The full new file content (not a diff/patch) --
                reconcile the DUT instantiation's port connections against
                the real module's ports, keep the rest of the scaffolded
                structure as-is.
        """
        Path(self.top_v_path).write_text(content)
        return f"OK, wrote {len(content)} bytes to {self.top_v_path}"

    def read_dut_source(self) -> str:
        """The real target module's own RTL source, read-only -- use this
        to get exact port widths and confirm the real module name."""
        return Path(self.dut_source_path).read_text()


class RtlEditTool(ChiaTool):
    """MCP tool for one ``rtl`` task: read the design's sources and record
    text replacements in them.

    Exposes ``{name}_list``, ``{name}_read``, ``{name}_search``, and
    ``{name}_replace``. Paths are relative to the checkout and must lie
    under :data:`RTL_EDIT_ROOT`. A replacement never writes the checkout:
    the tool keeps each changed file's full new content and rewrites
    *staging_path*, a JSON file mace.integrator passes to
    ``OpenPitonWorkspaceNode.apply_edits`` once the agent's call ends. A
    read of a file the agent already changed shows the recorded content.
    Like TestbenchEditTool, it reads the checkout's files directly, so its
    actor must run on the machine that holds the checkout (pass the node's
    ``task_options``).
    """

    def __init__(self, name: str, piton_root: str, staging_path: str, task_options: dict | None = None):
        super().__init__(name, task_options=task_options)
        self.piton_root = str(Path(piton_root))
        self.staging_path = str(Path(staging_path))
        self.staged: dict[str, str] = {}
        self.mcp.add_tool(self.list_dir, name=f"{name}_list")
        self.mcp.add_tool(self.read, name=f"{name}_read")
        self.mcp.add_tool(self.search, name=f"{name}_search")
        self.mcp.add_tool(self.replace, name=f"{name}_replace")
        super().__post_init__()

    def _resolve(self, path: str) -> tuple[str, Path]:
        """(checkout-relative path, absolute path) for *path*; ValueError
        unless it lies under RTL_EDIT_ROOT inside the checkout."""
        rel = os.path.normpath(path.strip().lstrip("/")).replace(os.sep, "/")
        if rel != RTL_EDIT_ROOT and not rel.startswith(RTL_EDIT_ROOT + "/"):
            raise ValueError(f"{path!r} is outside {RTL_EDIT_ROOT}/, the only part of the checkout you may change")
        full = Path(self.piton_root) / rel
        root = os.path.realpath(self.piton_root)
        if not os.path.realpath(full).startswith(root + os.sep):
            raise ValueError(f"{path!r} resolves outside the checkout")
        return rel, full

    def _content(self, rel: str, full: Path) -> str:
        if rel in self.staged:
            return self.staged[rel]
        return full.read_text(encoding="utf-8", errors="surrogateescape")

    def list_dir(self, path: str = RTL_EDIT_ROOT) -> str:
        """List one directory under piton/design, directories marked with a
        trailing slash.

        Args:
            path: The directory, relative to the checkout root.
        """
        try:
            rel, full = self._resolve(path)
            names = sorted(p.name + ("/" if p.is_dir() else "") for p in full.iterdir())
        except (OSError, ValueError) as e:
            return f"ERROR: {e}"
        return f"{rel}/:\n" + "\n".join(names)

    def read(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        """Read a source file under piton/design, with line numbers, at most
        400 lines per call.

        Args:
            path: The file, relative to the checkout root.
            start_line: The first line to show, counting from 1.
            end_line: The last line to show; 0 shows up to 400 lines.
        """
        try:
            rel, full = self._resolve(path)
            lines = self._content(rel, full).splitlines()
        except (OSError, ValueError) as e:
            return f"ERROR: {e}"
        first = max(start_line, 1)
        last = min(end_line or first + _READ_LINES - 1, first + _READ_LINES - 1, len(lines))
        shown = "\n".join(f"{n}: {lines[n - 1]}" for n in range(first, last + 1))
        return f"{rel}, lines {first}-{last} of {len(lines)}:\n{shown}"

    def search(self, pattern: str, path: str = RTL_EDIT_ROOT) -> str:
        """Search files under piton/design for an extended regular
        expression, at most 60 matches. Changes you recorded are not
        searched; read the file to see them.

        Args:
            pattern: The regular expression, as for grep -E.
            path: The file or directory to search, relative to the checkout root.
        """
        try:
            _, full = self._resolve(path)
            done = subprocess.run(
                ["grep", "-rnIE", "-e", pattern, "--", str(full)],
                capture_output=True, text=True, errors="replace", timeout=60,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return f"ERROR: {e}"
        prefix = self.piton_root.rstrip("/") + "/"
        hits = [line.removeprefix(prefix)[:300] for line in done.stdout.splitlines()]
        more = f"\n({len(hits) - _SEARCH_HITS} more matches)" if len(hits) > _SEARCH_HITS else ""
        return ("\n".join(hits[:_SEARCH_HITS]) + more) if hits else "no matches"

    def replace(self, path: str, old: str, new: str) -> str:
        """Replace one exact snippet of a source file under piton/design.
        The change is built and every gate workload simulated after you
        finish; you cannot run anything yourself.

        Args:
            path: The file, relative to the checkout root.
            old: Text that occurs exactly once in the file, copied exactly,
                including indentation; include enough lines to make it unique.
            new: The text to put in its place.
        """
        try:
            rel, full = self._resolve(path)
            content = self._content(rel, full)
        except (OSError, ValueError) as e:
            return f"ERROR: {e}"
        if not old:
            return "ERROR: old is empty"
        count = content.count(old)
        if count != 1:
            return f"ERROR: old occurs {count} times in {rel}; it must occur exactly once"
        self.staged[rel] = content.replace(old, new, 1)
        os.makedirs(os.path.dirname(self.staging_path), exist_ok=True)
        partial = self.staging_path + ".tmp"
        with open(partial, "w", encoding="utf-8", errors="surrogateescape") as f:
            json.dump(self.staged, f)
        os.replace(partial, self.staging_path)
        return f"OK: recorded the change to {rel}; {len(self.staged)} file(s) changed so far"
