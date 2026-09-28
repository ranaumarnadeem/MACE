"""mace.codesign -- search cache geometry and interconnect under the simulation check.

A co-design task fixes a core, a mesh, and gate workloads, and asks for the
design that finishes them soonest within a cache-area budget.

- :mod:`mace.codesign.space`: a design (four cache geometries and the
  interconnect) and the space of allowed designs.
- :mod:`mace.codesign.area`: each design's cache area, from CHIA's SRAM
  models.
- :mod:`mace.codesign.search`: the strategies that pick the next designs:
  MACE's LLM proposer, random search, grid search, and Bayesian
  optimization.
- :mod:`mace.codesign.run`: the search loop. Every design is built and
  checked through the loop's own build-and-check path, and scored on the
  simulated time at which its gate workloads finish.
"""
