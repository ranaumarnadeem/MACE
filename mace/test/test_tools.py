"""Tier-0 tests for mace.tools.TestbenchEditTool and RtlEditTool: no Ray needed.

Run:
    pytest mace/test/test_tools.py -q

Same technique chia_openpiton/test/test_tools_local.py uses: a bare
instance built with object.__new__, skipping ChiaTool.__init__/__post_init__
(the only parts that touch Ray) entirely, since read_testbench/
write_testbench/read_dut_source are plain file I/O with no Ray dependency
of their own.
"""

from __future__ import annotations

import inspect
import json

import pytest

from mace.tools import DirectedTestTool, RtlEditTool, TestbenchEditTool


def bare_tool(top_v_path: str, dut_source_path: str = "") -> TestbenchEditTool:
    tool = object.__new__(TestbenchEditTool)
    tool.name = "unit_test_edit"
    tool.top_v_path = top_v_path
    tool.dut_source_path = dut_source_path
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
        params = list(inspect.signature(TestbenchEditTool.write_testbench).parameters)
        assert params == ["self", "content"]

    def test_read_testbench_also_has_no_path_parameter(self):
        params = list(inspect.signature(TestbenchEditTool.read_testbench).parameters)
        assert params == ["self"]


class TestReadDutSource:
    def test_returns_the_dut_source_content(self, tmp_path):
        rtl = tmp_path / "foo.v"
        rtl.write_text("module foo (\n  input clk\n);\nendmodule\n")
        tool = bare_tool(str(tmp_path / "foo_ut_top.v"), dut_source_path=str(rtl))

        assert tool.read_dut_source() == "module foo (\n  input clk\n);\nendmodule\n"

    def test_has_no_path_parameter(self):
        params = list(inspect.signature(TestbenchEditTool.read_dut_source).parameters)
        assert params == ["self"]

    def test_does_not_expose_a_write_method(self):
        """Read-only, deliberately: reconciling a testbench is never a
        reason to edit the module it targets."""
        assert not hasattr(TestbenchEditTool, "write_dut_source")


@pytest.fixture
def rtl_tool(tmp_path):
    design = tmp_path / "piton" / "design" / "chip"
    design.mkdir(parents=True)
    (design / "adapter.sv").write_text("module adapter;\n  assign inv = vld;\n  assign ack = 1'b0;\nendmodule\n")
    (tmp_path / "piton" / "verif" / "env").mkdir(parents=True)
    (tmp_path / "piton" / "verif" / "env" / "monitor.v").write_text("module monitor; endmodule\n")
    tool = object.__new__(RtlEditTool)
    tool.name = "rtl_edit"
    tool.piton_root = str(tmp_path)
    tool.staging_path = str(tmp_path / "build" / ".mace_edit_staging" / "t.json")
    tool.staged = {}
    return tool


class TestRtlEditTool:
    def test_read_numbers_the_lines(self, rtl_tool):
        out = rtl_tool.read("piton/design/chip/adapter.sv", start_line=2, end_line=2)
        assert out.endswith("2:   assign inv = vld;")
        assert "lines 2-2 of 4" in out

    def test_paths_outside_the_design_are_refused(self, rtl_tool):
        assert rtl_tool.read("piton/verif/env/monitor.v").startswith("ERROR:")
        assert rtl_tool.replace("piton/verif/env/monitor.v", "module", "x").startswith("ERROR:")
        assert rtl_tool.read("piton/design/../verif/env/monitor.v").startswith("ERROR:")

    def test_replace_stages_the_whole_file_and_leaves_the_checkout_alone(self, rtl_tool, tmp_path):
        out = rtl_tool.replace("piton/design/chip/adapter.sv", "assign inv = vld;", "assign inv = vld & en;")

        assert out.startswith("OK")
        staged = json.loads((tmp_path / "build" / ".mace_edit_staging" / "t.json").read_text())
        assert staged["piton/design/chip/adapter.sv"].splitlines()[1] == "  assign inv = vld & en;"
        assert "vld & en" not in (tmp_path / "piton" / "design" / "chip" / "adapter.sv").read_text()

    def test_a_later_read_and_replace_see_the_staged_content(self, rtl_tool):
        rtl_tool.replace("piton/design/chip/adapter.sv", "assign inv = vld;", "assign inv = vld & en;")
        assert "vld & en" in rtl_tool.read("piton/design/chip/adapter.sv")
        assert rtl_tool.replace("piton/design/chip/adapter.sv", "vld & en", "vld | en").startswith("OK")

    def test_old_text_must_occur_exactly_once(self, rtl_tool):
        assert "occurs 2 times" in rtl_tool.replace("piton/design/chip/adapter.sv", "assign", "wire")
        assert "occurs 0 times" in rtl_tool.replace("piton/design/chip/adapter.sv", "missing", "x")
        assert rtl_tool.replace("piton/design/chip/adapter.sv", "", "x") == "ERROR: old is empty"

    def test_search_reports_paths_relative_to_the_checkout(self, rtl_tool):
        assert "piton/design/chip/adapter.sv:2:" in rtl_tool.search("inv = vld")
        assert rtl_tool.search("nothing_matches_this") == "no matches"

    def test_list_marks_directories(self, rtl_tool):
        assert "chip/" in rtl_tool.list_dir("piton/design")


ASM_TEST = """#include "riscv_test.h"
#include "test_macros.h"
RVTEST_RV32U
RVTEST_CODE_BEGIN
  TEST_CASE( 2, x2, 1, li x2, 1; )
  TEST_PASSFAIL
RVTEST_CODE_END
  .data
RVTEST_DATA_BEGIN
  TEST_DATA
RVTEST_DATA_END
"""
C_TEST = "#include <stdio.h>\nint main(int argc, char **argv) {\n  return 0;\n}\n"
ALU = "piton/design/chip/alu.v"


def make_test_tool(tmp_path, form="S"):
    (tmp_path / "piton" / "design" / "chip").mkdir(parents=True)
    (tmp_path / "piton" / "design" / "chip" / "alu.v").write_text("module alu;\nendmodule\n")
    (tmp_path / "piton" / "verif" / "diag").mkdir(parents=True)
    (tmp_path / "piton" / "verif" / "diag" / "lw.S").write_text("# an example\n")
    (tmp_path / "piton" / "verif" / "env").mkdir(parents=True)
    (tmp_path / "piton" / "verif" / "env" / "monitor.v").write_text("module monitor; endmodule\n")
    tool = object.__new__(DirectedTestTool)
    tool.name = "dtest"
    tool.piton_root = str(tmp_path)
    tool.test_dir = str(tmp_path / "build" / ".mace_tests" / "abc12345")
    tool.form = form
    tool.tests = {}
    return tool


class TestDirectedTestToolReads:
    def test_it_reads_the_design_and_the_example_diags(self, tmp_path):
        tool = make_test_tool(tmp_path)
        assert tool.read(ALU, 1, 1).endswith("1: module alu;")
        assert "an example" in tool.read("piton/verif/diag/lw.S")
        assert "alu.v" in tool.list_dir("piton/design/chip")
        assert "chip/alu.v" in tool.search("module", "piton/design")

    @pytest.mark.parametrize(
        "path", ["piton/verif/env/monitor.v", "../outside.v", "/etc/passwd", "mace/workloads/matmul.c"]
    )
    def test_it_does_not_read_outside_the_design_and_the_diags(self, tmp_path, path):
        tool = make_test_tool(tmp_path)
        assert tool.read(path).startswith("ERROR")
        assert tool.list_dir(path).startswith("ERROR")
        assert tool.search("module", path).startswith("ERROR")


class TestDirectedTestToolWrites:
    def test_a_test_is_saved_as_dt_name_with_a_sidecar(self, tmp_path):
        tool = make_test_tool(tmp_path)
        assert tool.write_test("lh_sign", ALU, ASM_TEST).startswith("OK")
        written = tmp_path / "build" / ".mace_tests" / "abc12345"
        assert (written / "dt_lh_sign.S").read_text() == ASM_TEST
        (entry,) = json.loads((written / "tests.json").read_text())
        assert entry == {"name": "lh_sign", "target": ALU, "form": "S", "source": ASM_TEST}

    def test_a_c_test_is_saved_with_the_c_extension(self, tmp_path):
        tool = make_test_tool(tmp_path, form="c")
        assert tool.write_test("count", ALU, C_TEST).startswith("OK")
        assert (tmp_path / "build" / ".mace_tests" / "abc12345" / "dt_count.c").is_file()

    def test_writing_a_name_again_replaces_the_test(self, tmp_path):
        tool = make_test_tool(tmp_path)
        tool.write_test("t", ALU, ASM_TEST)
        tool.write_test("t", ALU, ASM_TEST.replace("li x2, 1", "li x2, 2"))
        entries = json.loads((tmp_path / "build" / ".mace_tests" / "abc12345" / "tests.json").read_text())
        assert len(entries) == 1 and "li x2, 2" in entries[0]["source"]

    def test_it_keeps_at_most_three_tests(self, tmp_path):
        tool = make_test_tool(tmp_path)
        for n in range(3):
            assert tool.write_test(f"t{n}", ALU, ASM_TEST).startswith("OK")
        assert tool.write_test("t3", ALU, ASM_TEST).startswith("ERROR: at most 3")
        assert tool.write_test("t0", ALU, ASM_TEST).startswith("OK")

    @pytest.mark.parametrize("name", ["", "Bad", "has-dash", "../x", "1abc", "a" * 41, "x.S"])
    def test_a_bad_name_is_refused(self, tmp_path, name):
        tool = make_test_tool(tmp_path)
        assert tool.write_test(name, ALU, ASM_TEST).startswith("ERROR: name")
        assert not (tmp_path / "build").exists()

    @pytest.mark.parametrize(
        "target", ["piton/verif/env/monitor.v", "piton/design/chip/missing.v", "piton/design/chip", "../alu.v"]
    )
    def test_the_target_must_be_an_existing_design_file(self, tmp_path, target):
        tool = make_test_tool(tmp_path)
        assert tool.write_test("t", target, ASM_TEST).startswith("ERROR: target")
        assert not (tmp_path / "build").exists()

    def test_the_source_must_look_like_the_form(self, tmp_path):
        asm = make_test_tool(tmp_path, "S")
        assert "RVTEST_CODE_BEGIN" in asm.write_test("t", ALU, "int main() {}")
        assert "TEST_PASSFAIL" in asm.write_test("t", ALU, "RVTEST_CODE_BEGIN\n")
        assert asm.write_test("t", ALU, "").startswith("ERROR")
        c = make_test_tool(tmp_path / "c", "c")
        assert "main" in c.write_test("t", ALU, "RVTEST_CODE_BEGIN TEST_PASSFAIL")
        assert "limit" in c.write_test("t", ALU, C_TEST + "/*" + "x" * 31000 + "*/")

    def test_the_constructor_refuses_a_directory_outside_the_test_area(self, tmp_path):
        with pytest.raises(ValueError, match="test_dir"):
            DirectedTestTool("d", str(tmp_path), str(tmp_path / "piton" / "design"), "S")
        with pytest.raises(ValueError, match="test_dir"):
            DirectedTestTool("d", str(tmp_path), str(tmp_path / "build" / ".mace_tests"), "S")
        with pytest.raises(ValueError, match="form"):
            DirectedTestTool("d", str(tmp_path), str(tmp_path / "build" / ".mace_tests" / "x"), "v")

    def test_the_write_method_takes_no_path_for_the_file(self):
        assert list(inspect.signature(DirectedTestTool.write_test).parameters) == ["self", "name", "target", "source"]
