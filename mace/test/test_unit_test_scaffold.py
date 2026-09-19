"""Tier-0 tests for mace.unit_test_scaffold.

Run:
    pytest mace/test/test_unit_test_scaffold.py -q
"""

from __future__ import annotations

import pytest

from mace.unit_test_scaffold import (
    ModuleNotFoundError_,
    _fix_config_for_verilator,
    extract_module_port_list,
    module_name_from_path,
    parse_port_names,
    read_dut_ports,
    scaffold_env,
    unit_test_env_name,
)

# The exact shape create_env.py generates -- captured for real (twice, from
# two different real --name= values) in mace.unit_test_scaffold.scaffold_env's
# own docstring; {name} is create_env.py's own substitution point.
REAL_CONFIG_TEMPLATE = """\
// Tesbench configuration file for the {name} environment

<{name}>
    -model={name}
    // TODO: Specify top level module(s) to be simulated
    -toplevel={name}_top
    // TODO: Change the flist file for the DUT which specifies all
    //       the source files for your DUT if it is not correct.
    -flist=$DV_ROOT/design/{name}/rtl/Flist.{name}
    // TODO: Add flist files for any other modules your DUT depends on.
    //       For example:
    //
    //               -flist=$DV_ROOT/design/common/rtl/Flist.clib_common
    -flist=$DV_ROOT/verif/env/{name}/{name}.flist
    -flist=$DV_ROOT/verif/env/test_infrstrct/test_infrstrct_include.flist
    -env_base=$DV_ROOT/verif/env/{name}
    -vcs_build_args=+incdir+$DV_ROOT/verif/env/test_infrstrct/
    -vcs_build_args=+notimingcheck
    -vcs_build_args=+nospecify
    -vcs_build_args=+nbaopt
    -vcs_build_args=-Xstrict=1 -notice
    -sim_run_args=+test_cases_path=$DV_ROOT/verif/env/{name}/test_cases/
</{name}>
"""

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

    def test_module_dv_path_fixes_the_generated_config_and_flist(self, tmp_path, monkeypatch):
        """module_dv_path=None (the default, used above) leaves create_env.py's
        raw output untouched -- this is the real, found-by-building fix path,
        exercised against the real captured template shape."""
        piton_root = tmp_path
        dv = piton_root / "piton"
        env_dir = dv / "verif" / "env" / "foo_ut"
        config_dir = dv / "tools" / "src" / "sims"

        def fake_run(cmd, **kw):
            env_dir.mkdir(parents=True)
            (env_dir / "test_cases").mkdir()
            (env_dir / "foo_ut_top.v").write_text("module foo_ut_top;\nendmodule\n")
            (env_dir / "foo_ut.flist").write_text(
                "// Flist for foo_ut testbench environment\n\nfoo_ut_top.v"
            )
            config_dir.mkdir(parents=True)
            (config_dir / "foo_ut.config").write_text(REAL_CONFIG_TEMPLATE.format(name="foo_ut"))

            class FakeResult:
                returncode = 0
                stdout = "created foo_ut"
                stderr = ""

            return FakeResult()

        monkeypatch.setattr("subprocess.run", fake_run)
        scaffold_env(str(piton_root), "foo_ut", module_dv_path="design/common/rtl/foo.v")

        config_text = (config_dir / "foo_ut.config").read_text()
        assert "-flist=$DV_ROOT/design/foo_ut/rtl/Flist.foo_ut" not in config_text
        assert "-env_base=" not in config_text
        assert "-vcs_build_args=+notimingcheck" not in config_text
        assert "-sim_build_args=+incdir+$DV_ROOT/verif/env/test_infrstrct/" in config_text

        flist_text = (env_dir / "foo_ut.flist").read_text()
        assert "$DV_ROOT/design/common/rtl/foo.v" in flist_text
        assert "foo_ut_top.v" in flist_text  # the testbench's own file is still there too

    def test_module_dv_path_is_a_noop_when_env_already_exists(self, tmp_path, monkeypatch):
        env_dir = tmp_path / "piton" / "verif" / "env" / "foo_ut"
        env_dir.mkdir(parents=True)

        def fail_if_called(*a, **kw):
            raise AssertionError("subprocess.run should not be called when already scaffolded")

        monkeypatch.setattr("subprocess.run", fail_if_called)
        result = scaffold_env(str(tmp_path), "foo_ut", module_dv_path="design/common/rtl/foo.v")
        assert result["created"] is False


class TestFixConfigForVerilator:
    def test_removes_the_broken_placeholder_flist_line(self):
        fixed = _fix_config_for_verilator(REAL_CONFIG_TEMPLATE.format(name="foo_ut"), "foo_ut")
        assert "-flist=$DV_ROOT/design/foo_ut/rtl/Flist.foo_ut" not in fixed
        # the testbench's own real flist line must survive
        assert "-flist=$DV_ROOT/verif/env/foo_ut/foo_ut.flist" in fixed

    def test_removes_env_base(self):
        fixed = _fix_config_for_verilator(REAL_CONFIG_TEMPLATE.format(name="foo_ut"), "foo_ut")
        assert "-env_base=" not in fixed

    def test_removes_vcs_only_flags(self):
        fixed = _fix_config_for_verilator(REAL_CONFIG_TEMPLATE.format(name="foo_ut"), "foo_ut")
        for flag in ("+notimingcheck", "+nospecify", "+nbaopt", "-Xstrict=1"):
            assert flag not in fixed

    def test_translates_the_one_real_incdir_flag(self):
        fixed = _fix_config_for_verilator(REAL_CONFIG_TEMPLATE.format(name="foo_ut"), "foo_ut")
        assert "-sim_build_args=+incdir+$DV_ROOT/verif/env/test_infrstrct/" in fixed

    def test_sim_run_args_line_survives_untouched(self):
        fixed = _fix_config_for_verilator(REAL_CONFIG_TEMPLATE.format(name="foo_ut"), "foo_ut")
        assert "-sim_run_args=+test_cases_path=$DV_ROOT/verif/env/foo_ut/test_cases/" in fixed
