"""Tier-0 tests for mace.unit_test_scaffold.

Run:
    pytest mace/test/test_unit_test_scaffold.py -q
"""

from __future__ import annotations

import pytest

from mace.unit_test_scaffold import (
    ModuleNotFoundError_,
    extract_module_port_list,
    module_name_from_path,
    parse_port_names,
    read_dut_ports,
    scaffold_env,
    unit_test_env_name,
)

SIMPLE_MODULE = """
module foo (
    input        clk,
    input        rst_n,
    output reg   done
);
endmodule
"""

PARAMETERIZED_MODULE = """
module bar #(
    parameter WIDTH = 8
) (
    input  wire             clk,
    input  wire [WIDTH-1:0] data_in,
    output reg  [WIDTH-1:0] data_out
);
endmodule
"""

MULTI_NAME_LINE_MODULE = """
module baz (
    input clk, reset_l,
    output reg trap
);
endmodule
"""

IFDEF_MODULE = """
module qux (
    input clk,
`ifdef SOME_FEATURE
    output reg feature_flag,
`endif
    output reg done
);
endmodule
"""


class TestExtractModulePortList:
    def test_simple_module(self):
        text = extract_module_port_list(SIMPLE_MODULE, "foo")
        assert "clk" in text
        assert "done" in text

    def test_module_not_found_raises(self):
        with pytest.raises(ModuleNotFoundError_):
            extract_module_port_list(SIMPLE_MODULE, "nonexistent")

    def test_skips_parameter_block(self):
        text = extract_module_port_list(PARAMETERIZED_MODULE, "bar")
        assert "WIDTH" not in text or "parameter" not in text
        assert "data_in" in text
        assert "data_out" in text


class TestParsePortNames:
    def test_simple_module(self):
        names = parse_port_names(extract_module_port_list(SIMPLE_MODULE, "foo"))
        assert names == ["clk", "rst_n", "done"]

    def test_parameterized_module_widths_stripped(self):
        names = parse_port_names(extract_module_port_list(PARAMETERIZED_MODULE, "bar"))
        assert names == ["clk", "data_in", "data_out"]

    def test_multi_name_line(self):
        names = parse_port_names(extract_module_port_list(MULTI_NAME_LINE_MODULE, "baz"))
        assert names == ["clk", "reset_l", "trap"]

    def test_ifdef_block_is_included_not_dropped(self):
        # Over-inclusive by design (see module docstring) -- a conditional
        # port is still real information for the agent, not noise to hide.
        names = parse_port_names(extract_module_port_list(IFDEF_MODULE, "qux"))
        assert names == ["clk", "feature_flag", "done"]


class TestReadDutPorts:
    def test_reads_from_a_real_file(self, tmp_path):
        rtl = tmp_path / "foo.v"
        rtl.write_text(SIMPLE_MODULE)
        assert read_dut_ports(str(rtl), "foo") == ["clk", "rst_n", "done"]


class TestModuleNameFromPath:
    @pytest.mark.parametrize(
        "module_path,expected",
        [("picorv32.v", "picorv32"), ("/a/b/l15_pipeline.v.pyv", "l15_pipeline")],
    )
    def test_strips_all_extensions(self, module_path, expected):
        assert module_name_from_path(module_path) == expected


class TestUnitTestEnvName:
    @pytest.mark.parametrize(
        "module_path,expected",
        [("picorv32.v", "picorv32_ut"), ("/a/b/l15_pipeline.v.pyv", "l15_pipeline.v_ut")],
    )
    def test_derives_env_name_from_stem(self, module_path, expected):
        assert unit_test_env_name(module_path) == expected


class TestScaffoldEnv:
    def test_noop_when_already_scaffolded(self, tmp_path, monkeypatch):
        piton_root = tmp_path
        env_dir = piton_root / "piton" / "verif" / "env" / "foo_ut"
        env_dir.mkdir(parents=True)

        def fail_if_called(*a, **kw):
            raise AssertionError("subprocess.run should not be called when already scaffolded")

        monkeypatch.setattr("subprocess.run", fail_if_called)
        result = scaffold_env(str(piton_root), "foo_ut")
        assert result["created"] is False
        assert result["env_dir"] == str(env_dir)

    def test_invokes_create_env_when_missing(self, tmp_path, monkeypatch):
        piton_root = tmp_path
        calls = []

        class FakeResult:
            returncode = 0
            stdout = "created foo_ut"
            stderr = ""

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return FakeResult()

        monkeypatch.setattr("subprocess.run", fake_run)
        result = scaffold_env(str(piton_root), "foo_ut")
        assert result["created"] is True
        assert len(calls) == 1
        cmd, kw = calls[0]
        assert cmd[0] == "python3"
        assert "--name=foo_ut" in cmd
        assert kw["env"]["DV_ROOT"] == str(piton_root / "piton")

    def test_raises_on_nonzero_returncode(self, tmp_path, monkeypatch):
        class FakeResult:
            returncode = 2
            stdout = ""
            stderr = "DV_ROOT environment variable is not defined"

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeResult())
        with pytest.raises(RuntimeError, match="create_env.py"):
            scaffold_env(str(tmp_path), "foo_ut")
