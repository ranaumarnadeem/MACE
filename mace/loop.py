"""mace.loop -- the MACE agentic loop driver.

The full loop is Requirement -> Planning -> Task Decomposition -> Parallel
Agent Execution -> RTL/System Integration -> Verification -> Failure
Analysis -> Iteration. :func:`run_mace_step` is deliberately the smallest
slice of that: one task, no fan-out, no Planner. It proves the wiring
everything else depends on -- LLM dispatch, build, run, gate on the
transcript verdict -- against a real OpenPitonWorkspaceNode (a stubbed
``sims`` in tests), so that wiring is settled before any intelligence (a
Planner producing the task DAG, a failure-analysis agent replanning) is
layered on top of it.
"""

from __future__ import annotations

import os
from pathlib import Path

import ray

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import COVERAGE_LINE_FLAG, PitonConfig
from mace.spec import MaceSpec, StepResult, Task
from mace.tools import TestbenchEditTool
from mace.unit_test_scaffold import (
    ModuleNotFoundError_,
    module_name_from_path,
    read_dut_ports,
    scaffold_env,
    unit_test_env_name,
)
from mace.workloads import RECOMMENDED_RTL_TIMEOUT, WORKLOADS_DIR


def run_mace_step(
    piton_root: str, spec: MaceSpec, task: Task, llm, tools=(), asm_diag_root: str | None = None
) -> StepResult:
    """Run one task: an LLM turn, a build, then a run of the spec's first
    gate workload, gated on that run's transcript verdict.

    Against a real LLM backend and real tools, the edit ``task.spec``
    describes happens as a side effect inside ``llm.prompt()`` -- the
    backend's own tool-calling loop, not this function. Against FakeLLM in
    tests, ``prompt()`` just returns its next scripted response and no edit
    occurs, which is fine here: this function's job is to prove
    build -> run -> gate wiring, not that an LLM can write RTL.

    Only the spec's first workload is checked -- gating on every workload is
    fan-out (mace.spec.MaceSpec.workloads plural exists for that), which
    belongs to a later, multi-task version of this loop.

    ``asm_diag_root`` defaults to mace's own ``workloads/`` directory, since
    a spec's workloads are normally one of the frozen gate programs there --
    ``sims`` searches it as an *extra* directory alongside the checkout's
    own diags, so an OpenPiton-native test name (e.g. ``hello_world.c``)
    still resolves fine with the default in place.

    A ``unit_test``-kind task takes a different path entirely -- see
    :func:`_run_unit_test_step`.
    """
    if task.kind == "unit_test":
        return _run_unit_test_step(piton_root, task, llm, tools)

    query = llm.prompt(task.spec, tools=list(tools))

    config = _config_for_task(spec, task)
    build = OpenPitonWorkspaceNode.build(piton_root, config)
    if not build.success:
        return StepResult(task=task, query=query, build=build, run=None, passed=False)

    run = OpenPitonWorkspaceNode.run(
        piton_root,
        config,
        spec.workloads[0],
        asm_diag_root=str(WORKLOADS_DIR) if asm_diag_root is None else asm_diag_root,
        rtl_timeout=RECOMMENDED_RTL_TIMEOUT,
    )
    return StepResult(task=task, query=query, build=build, run=run, passed=run.success)


def _run_unit_test_step(piton_root: str, task: Task, llm, tools) -> StepResult:
    """Scaffold-then-adapt-then-build path for a ``unit_test``-kind task.

    ``task.spec`` names the target module's RTL path, relative to
    ``piton_root`` (e.g. ``piton/design/chip/tile/pico/rtl/picorv32.v``).
    Scaffolding a fresh environment is mechanical (create_env.py, already
    idempotent); reconciling the scaffolded testbench's dummy DUT
    connections against the module's real ports is the "small edit" -- this
    function hands the agent that reconciliation as a prompt (the real port
    list, the file to edit), it does not do the edit itself. Whether that
    edit actually happens depends on ``tools`` carrying real file-editing
    capability, same as every other kind here.

    Gated on build success only, never a run verdict: every environment
    built this way shares piton/verif/env/test_infrstrct/test_infrstrct.v
    with this project's own pico_reset_ut (see scripts/patch_openpiton.sh),
    which has a real, documented, deliberately deferred Verilator
    incompatibility on the RUN side. Reporting a run verdict here would
    misattribute that known, pre-existing gap to this task.

    When Ray is actually initialized (a real run, never a tier-0 test --
    see mace.tools.TestbenchEditTool's own module docstring for why this is
    a purpose-built, single-file-scoped tool rather than a general BashTool),
    this constructs one and adds it to *tools* for the duration of the LLM
    call, then tears it down -- it is per-task (scoped to this task's own
    scaffolded file, only known once :func:`scaffold_env` has run), so it
    cannot be constructed once by a caller and reused the way a general
    tool can.
    """
    module_path = task.spec.strip()
    env_name = unit_test_env_name(module_path)
    rtl_path = str(Path(piton_root) / module_path)
    module_dv_path = os.path.relpath(rtl_path, str(Path(piton_root) / "piton"))
    scaffold_result = scaffold_env(piton_root, env_name, module_dv_path=module_dv_path)

    module_name = module_name_from_path(module_path)
    try:
        ports = read_dut_ports(rtl_path, module_name)
        ports_desc = ", ".join(ports)
    except (OSError, ModuleNotFoundError_) as e:
        ports_desc = f"(could not read real ports: {e})"

    prompt = (
        f"Scaffolded a new unit-test environment '{env_name}' for module "
        f"'{module_name}' at {rtl_path}.\n"
        f"The real module's ports, in declaration order: {ports_desc}\n"
        f"Edit piton/verif/env/{env_name}/{env_name}_top.v (via the edit "
        f"tool, if one is available -- it can also read the real DUT source "
        f"at {rtl_path} for exact port widths) so it actually builds against "
        f"the real module. create_env.py's generic template needs several "
        f"things fixed, all mechanical, not just port names:\n"
        f"1. The DUT instantiation's module type is currently "
        f"'{env_name}' (the environment's own name) -- change it to the "
        f"real module name, '{module_name}'.\n"
        f"2. Its port connections are generic placeholders (.input0(...), "
        f".output0(...)) -- reconcile them to the real port names/widths.\n"
        f"3. SRC_BIT_WIDTH/SINK_BIT_WIDTH (and the matching #() params on "
        f"the test_source/test_sink instances) are undefined placeholders -- "
        f"set them to the real sum of input widths (excluding clk/rst_n) "
        f"and output widths respectively.\n"
        f"4. SRC_ENTRIES/SRC_LOG2_ENTRIES/SINK_ENTRIES/SINK_LOG2_ENTRIES are "
        f"also undefined -- any small consistent pair works (e.g. entries=8, "
        f"log2_entries=3) unless the test case needs more vectors.\n"
        f"5. NUM_TEST_CASES in the closing `TEST_INFRSTRCT_END(NUM_TEST_CASES) "
        f"is undefined -- replace it with the actual number of "
        f"`TEST_CASE_BEGIN blocks in the file.\n"
        f"Keep the rest of the scaffolded testbench structure as-is."
    )

    edit_tool = None
    if ray.is_initialized():
        edit_tool = TestbenchEditTool(f"unit_test_edit_{task.id}", scaffold_result["top_v"], rtl_path)
    all_tools = (*tools, edit_tool) if edit_tool is not None else tools
    try:
        query = llm.prompt(prompt, tools=list(all_tools))
    finally:
        if edit_tool is not None:
            edit_tool.stop()

    build = OpenPitonWorkspaceNode.build(piton_root, PitonConfig(sys=env_name))
    return StepResult(task=task, query=query, build=build, run=None, passed=build.success)


def _config_for_task(spec: MaceSpec, task: Task) -> PitonConfig:
    """A PitonConfig for *task*: spec's core/mesh/coverage, plus the task's
    own cache override if it set one via a Planner CACHES: line (see
    mace.agents.parse_cache_overrides) -- a task with no override keeps the
    mesh's default cache geometry, same as before per-task overrides existed.
    """
    kwargs: dict = dict(
        core=spec.core, x_tiles=spec.target_mesh[0], y_tiles=spec.target_mesh[1],
        extra_flags=(COVERAGE_LINE_FLAG,) if spec.coverage else (),
    )
    if task.caches is not None:
        kwargs["caches"] = task.caches_dict
    return PitonConfig(**kwargs)
