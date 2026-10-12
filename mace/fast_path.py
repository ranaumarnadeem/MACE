"""mace.fast_path -- run a plan that builds one design the way the retry agent does.

A plan is a task DAG, and a ``config`` or ``workload`` task builds its
configuration and simulates every gate workload. When all of a plan's tasks
resolve to the same build flags, they are one design with several names: the
first builds it, and each later task reuses that build and simulates the same
programs again. In the recorded main comparison, 75 of 123 iterations of the
full loop planned one design over one to seven tasks.

:func:`collapse_single_design` keeps the last task of such a plan, the one
that carries every inherited define. The orchestrator also turns the tasks'
own LLM calls off for it: the reply of such a call changes nothing that is
built, and those calls were $2.66 of the loop's $3.37 over 95 passing runs.
A plan with more than one design keeps its tasks and its calls.
"""

from __future__ import annotations

import dataclasses

from mace.integrator import topological_order
from mace.plan_check import config_key
from mace.spec import MaceSpec, Task

# The tasks that build the whole chip and simulate the gate workloads.
_BUILD_KINDS = ("config", "workload")


def collapse_single_design(spec: MaceSpec, tasks: tuple[Task, ...]) -> tuple[tuple[Task, ...], tuple[str, ...]] | None:
    """The one task that stands for *tasks*, and the ids it replaces, or
    ``None`` when *tasks* are not a single design.

    A plan is a single design when it has tasks, every one is a ``config`` or
    ``workload`` task, and all of them build the same flags under *spec*.
    The kept task is the last in dependency order, with no dependencies.
    """
    if not tasks or any(t.kind not in _BUILD_KINDS for t in tasks):
        return None
    if len({config_key(spec, t) for t in tasks}) != 1:
        return None
    last = topological_order(tasks)[-1]
    dropped = tuple(t.id for t in tasks if t.id != last.id)
    return (dataclasses.replace(last, deps=()),), dropped
