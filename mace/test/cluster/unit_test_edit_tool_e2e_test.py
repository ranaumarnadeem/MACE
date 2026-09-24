"""Tier-1 test: a real, Ray-backed TestbenchEditTool actually edits a real
file, end to end through mace.loop's unit_test dispatch path.

Run:
    pytest mace/test/cluster/unit_test_edit_tool_e2e_test.py -q

FakeLLM never invokes the tools it's given (it's purely scripted -- see its
own docstring), so proving "an agent can really use this tool to edit the
file" needs something that actually calls the tool, the way a real backend's
tool-calling loop would. EditingFakeLLM below does exactly that: it calls
write_testbench(...) on the last tool it's handed, for real, before
returning its scripted response -- the closest thing to a real agent action
this project can exercise without spending on a live LLM call.
"""

from __future__ import annotations

import stat

import pytest

ray = pytest.importorskip("ray")

from chia_openpiton.test.conftest import STUB_SETTINGS, STUB_SIMS  # noqa: E402

from mace.loop import run_mace_step  # noqa: E402
from mace.spec import MaceSpec, Task  # noqa: E402
from mace.test.conftest import FakeLLM  # noqa: E402
from mace.tools import TestbenchEditTool  # noqa: E402
from mace.unit_test_scaffold import unit_test_env_name  # noqa: E402

RTL_SPEC = "design/foo.v"
# Derived, not hardcoded: pre-creating any other env name makes scaffold_env
# miss it and fall through to create_env.py, which the stub checkout lacks.
ENV_NAME = unit_test_env_name(RTL_SPEC)
NEW_CONTENT = f"module {ENV_NAME}_top;\n  // reconciled by the agent\nendmodule\n"


class EditingFakeLLM(FakeLLM):
    """Like FakeLLM, but actually calls write_testbench on the real tool
    it's given -- simulating a real backend's own tool-calling loop."""

    def prompt(self, user_message, tools=None):
        for tool in tools or []:
            if isinstance(tool, TestbenchEditTool):
                tool.write_testbench(NEW_CONTENT)
        return super().prompt(user_message, tools=tools)


@pytest.fixture(scope="module")
def ray_local():
    ray.init(address="local", ignore_reinit_error=True, log_to_driver=False)
    yield
    ray.shutdown()


def _make_stub_checkout(root, verdict: str = "pass") -> str:
    tools_bin = root / "piton" / "tools" / "bin"
    tools_bin.mkdir(parents=True)
    (root / "build").mkdir()
    sims = tools_bin / "sims"
    shebang, _, body = STUB_SIMS.partition("\n")
    sims.write_text(f"{shebang}\nexport FAKE_SIMS_VERDICT={verdict}\n{body}")
    sims.chmod(sims.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (root / "piton" / "piton_settings.bash").write_text(STUB_SETTINGS)
    return root


def make_spec(**override):
    kwargs = {"workloads": ("hello_world.c",), "objective": "unit test picorv32"}
    kwargs.update(override)
    return MaceSpec(**kwargs)


class TestRealEditToolWiring:
    def test_agent_edit_actually_reaches_the_real_file(self, tmp_path, ray_local):
        root = _make_stub_checkout(tmp_path)
        env_dir = root / "piton" / "verif" / "env" / ENV_NAME
        env_dir.mkdir(parents=True)
        top_v = env_dir / f"{ENV_NAME}_top.v"
        top_v.write_text(f"module {ENV_NAME}_top;\n  // TODO: reconcile ports\nendmodule\n")
        rtl = root / RTL_SPEC
        rtl.parent.mkdir(parents=True)
        rtl.write_text("module foo (\n  input clk,\n  output reg done\n);\nendmodule\n")

        llm = EditingFakeLLM(responses=["reconciled the ports"])
        task = Task(id="t1", deps=(), kind="unit_test", spec=RTL_SPEC)

        result = run_mace_step(str(root), make_spec(), task, llm)

        assert top_v.read_text() == NEW_CONTENT
        assert result.build.success is True

    def test_tool_is_stopped_after_the_step_no_leaked_actor(self, tmp_path, ray_local):
        root = _make_stub_checkout(tmp_path)
        env_dir = root / "piton" / "verif" / "env" / ENV_NAME
        env_dir.mkdir(parents=True)
        (env_dir / f"{ENV_NAME}_top.v").write_text(f"module {ENV_NAME}_top;\nendmodule\n")
        rtl = root / RTL_SPEC
        rtl.parent.mkdir(parents=True)
        rtl.write_text("module foo (\n  input clk\n);\nendmodule\n")

        captured = []

        class CapturingFakeLLM(FakeLLM):
            def prompt(self, user_message, tools=None):
                captured.extend(tools or [])
                return super().prompt(user_message, tools=tools)

        llm = CapturingFakeLLM(responses=["ok"])
        task = Task(id="t1", deps=(), kind="unit_test", spec=RTL_SPEC)

        run_mace_step(str(root), make_spec(), task, llm)

        assert len(captured) == 1
        edit_tool = captured[0]
        assert isinstance(edit_tool, TestbenchEditTool)
        assert edit_tool._server_actor is None  # stop() clears this, see ChiaTool.stop
