"""mace.tools -- the agent-facing MCP tool for adapting a scaffolded
unit-test testbench (the "small edit" step mace.loop._run_unit_test_step
hands off to an LLM: reconciling a create_env.py-scaffolded testbench's
dummy DUT port connections against a target module's real ports).

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

from pathlib import Path

from chia.base.tools.ChiaTool import ChiaTool


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
