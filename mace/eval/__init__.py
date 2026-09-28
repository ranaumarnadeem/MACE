"""mace.eval -- the evaluation harness: task suite, batch runner, and report.

- :mod:`mace.eval.suite` loads and checks the task suite file.
- :mod:`mace.eval.runner` expands the suite into jobs (task, method,
  repeat), runs each on an empty build cache, and records it.
- :mod:`mace.eval.report` reads the run databases back into tables.

``examples/eval_batch.py`` and ``examples/eval_report.py`` are the drivers.
"""
