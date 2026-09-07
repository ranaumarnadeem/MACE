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

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import PitonConfig
from mace.spec import MaceSpec, StepResult, Task
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
    """
    query = llm.prompt(task.spec, tools=list(tools))

    config = PitonConfig(
        core=spec.core, x_tiles=spec.target_mesh[0], y_tiles=spec.target_mesh[1]
    )
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
