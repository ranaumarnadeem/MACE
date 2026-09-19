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
    """MCP tool over exactly one file: a scaffolded unit-test testbench.

    Exposes ``{name}_read_testbench`` (current content) and
    ``{name}_write_testbench`` (overwrite with reconciled content) -- no
    other method, no path parameter, so an agent given only this tool can
    read and rewrite the one file it was constructed for and nothing else
    in the checkout.
    """

    # Not a pytest test class -- the name just happens to start with "Test"
    # (Testbench...); this tells pytest's collector to leave it alone.
    __test__ = False

    def __init__(self, name: str, top_v_path: str, task_options: dict | None = None):
        super().__init__(name, task_options=task_options)
        self.top_v_path = str(Path(top_v_path))
        self.mcp.add_tool(self.read_testbench, name=f"{name}_read_testbench")
        self.mcp.add_tool(self.write_testbench, name=f"{name}_write_testbench")
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
