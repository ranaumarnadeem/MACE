"""Tier-0 tests for mace.tools.TestbenchEditTool: no Ray needed.

Run:
    pytest mace/test/test_tools.py -q

Same technique chia_openpiton/test/test_tools_local.py uses: a bare
instance built with object.__new__, skipping ChiaTool.__init__/__post_init__
(the only parts that touch Ray) entirely, since read_testbench/
write_testbench are plain file I/O with no Ray dependency of their own.
"""

from __future__ import annotations

from mace.tools import TestbenchEditTool


def bare_tool(top_v_path: str) -> TestbenchEditTool:
    tool = object.__new__(TestbenchEditTool)
    tool.name = "unit_test_edit"
    tool.top_v_path = top_v_path
    return tool


class TestReadTestbench:
    def test_returns_the_current_file_content(self, tmp_path):
        top_v = tmp_path / "foo_ut_top.v"
        top_v.write_text("module foo_ut_top;\nendmodule\n")
        tool = bare_tool(str(top_v))

        assert tool.read_testbench() == "module foo_ut_top;\nendmodule\n"


class TestWriteTestbench:
    def test_overwrites_the_file_with_new_content(self, tmp_path):
        top_v = tmp_path / "foo_ut_top.v"
        top_v.write_text("old content")
        tool = bare_tool(str(top_v))

        tool.write_testbench("new content")

        assert top_v.read_text() == "new content"

    def test_returns_a_confirmation_naming_the_path_and_size(self, tmp_path):
        top_v = tmp_path / "foo_ut_top.v"
        top_v.write_text("x")
        tool = bare_tool(str(top_v))

        result = tool.write_testbench("hello")

        assert "5 bytes" in result
        assert str(top_v) in result

    def test_has_no_path_parameter_to_write_elsewhere(self):
        """The whole point of this tool over a general BashTool: there is
        no way to name a different file. Asserted here as a signature
        check so a future edit can't quietly add one without this failing."""
        import inspect

        params = list(inspect.signature(TestbenchEditTool.write_testbench).parameters)
        assert params == ["self", "content"]

    def test_read_testbench_also_has_no_path_parameter(self):
        import inspect

        params = list(inspect.signature(TestbenchEditTool.read_testbench).parameters)
        assert params == ["self"]
