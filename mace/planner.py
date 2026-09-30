"""mace.planner -- turns a MaceSpec into a task DAG, via one LLM call.

The Planner is the first "intelligence" piece in the loop: it has an
opinion about how to decompose an objective into tasks, expressed as a
prompt. Everything downstream (mace.integrator, mace.loop) was proven
against hand-built Task tuples before this module existed, and does not
care where its tasks came from -- only that they are well-formed, which
mace.integrator.topological_levels already knows how to check.
"""

from __future__ import annotations

from mace import usage
from mace.agents import parse_tasks
from mace.integrator import topological_levels
from mace.spec import MaceSpec, Task

# The run's inputs, in the words every LLM method's prompt uses. The
# baselines render them with render_inputs() too (see mace/baselines/), so
# a comparison differs in what each method does, never in what it is told.
_INPUTS_TEMPLATE = """\
Objective: {objective}
Target mesh: {x_tiles}x{y_tiles} tiles
Gate workloads (must all still pass after every task): {workloads}
"""

_TASK_RULES = """\
Break this into an ordered set of tasks. Respond with one line per task,
in exactly this format (a footer, not prose):

TASK: <id> | deps=<comma-separated task ids, or empty> | kind=config|workload|unit_test | <short instruction>

- <id> must be unique.
- deps must name only ids you also define here, and must not form a cycle.
- kind is "config" for a configuration/RTL change, "workload" for running
  or fixing a gate workload, "unit_test" for a standalone unit test of one
  RTL module (added or changed this run) separate from the whole-chip build
  -- for a unit_test task, the instruction must be just that module's RTL
  path relative to the checkout root (e.g. piton/design/chip/tile/pico/rtl/
  picorv32.v), nothing else.
- Emit at least one TASK: line. Nothing else you write is parsed, but keep
  the rest brief.
"""

# How a task asks for a non-default cache geometry or extra RTL defines.
# Shared with the baselines, whose one design per attempt is one task.
OVERRIDE_RULES = """\
If a task must build against a non-default cache geometry, also emit one
line naming that task's id (defaults if omitted: l1i=16384,4 l1d=8192,4
l15=8192,4 l2=65536,4):

CACHES: <task id> | <name>=<size>,<associativity> ...

Only l1i, l1d, l15, l2 are recognized names; size and associativity must be
positive integers. On Ariane, neither l1d nor l1i may have more ways than
l15, and a build that breaks this is refused. A task with no CACHES: line
keeps the mesh's default cache geometry.

If a task needs a specific RTL define set on top of the mesh's defaults --
for example because an earlier attempt's failure feedback identified one a
build needs -- also emit one line naming that task's id:

CONFIG_RTL: <task id> | <FLAG1> <FLAG2> ...

Each flag is an upper-snake-case identifier (e.g. CONFIG_DISABLE_BIST_CLEAR).
These add to the mesh's default RTL defines; they never replace them. A task
with no CONFIG_RTL: line keeps the mesh's default RTL defines only.
"""


def render_inputs(spec: MaceSpec) -> str:
    """The objective, mesh, and gate workloads of *spec*, as every LLM
    method's prompt states them."""
    return _INPUTS_TEMPLATE.format(
        objective=spec.objective,
        x_tiles=spec.target_mesh[0],
        y_tiles=spec.target_mesh[1],
        workloads=", ".join(spec.workloads),
    )


class PlanningError(Exception):
    """The Planner's response produced no usable task DAG."""


def build_prompt(spec: MaceSpec, feedback: str = "") -> str:
    prompt = (
        f"You are planning the work for one MACE loop run against OpenPiton/{spec.core}.\n\n"
        f"{render_inputs(spec)}\n{_TASK_RULES}\n{OVERRIDE_RULES}"
    )
    if feedback:
        prompt += f"\nFeedback from a previous attempt, to inform this plan:\n{feedback}\n"
    return prompt


def plan(spec: MaceSpec, llm, tools=(), feedback: str = "") -> tuple[Task, ...]:
    """One LLM call, turned into a validated task DAG.

    ``feedback`` (from mace.triage.triage, via mace.loop's replan-on-failure
    loop) is appended as extra context for a replan attempt; empty for a
    first attempt.

    Raises:
        PlanningError: no ``TASK:`` lines, or the ones that parsed don't
            form a valid DAG (unknown dep, cycle). Both cases mean "not
            enough to act on" -- the same fail-open posture mace.agents'
            parsers document (see their module docstring). Deciding what a
            failed plan means (retry with more context, give up) is the
            caller's job; this only refuses to hand back something broken.
    """
    query = usage.prompt(llm, "plan", build_prompt(spec, feedback), tools)
    tasks = parse_tasks(query.result)
    if not tasks:
        raise PlanningError(f"no TASK: lines in the planner's response: {query.result!r}")
    try:
        topological_levels(tasks)
    except ValueError as e:
        raise PlanningError(f"planner produced an invalid task DAG: {e}") from e
    return tasks
