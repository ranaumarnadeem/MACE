"""mace.baselines -- the methods the evaluation compares with the full loop.

- :mod:`mace.baselines.expert`: the known passing configuration, built and
  checked once, with no LLM.
- :mod:`mace.baselines.one_shot`: the loop's planner, one plan, run once.
- :mod:`mace.baselines.retry_agent`: one agent that proposes one design per
  attempt and sees the raw errors of the last.

Each records its runs in the loop's own database, labelled with
:class:`mace.metrics.RunLabels`, and checks its designs through the loop's
own build-and-check path, so every method faces the same timeouts and the
same pass check. The prompts state a run's inputs with
:func:`mace.planner.render_inputs`.
"""
