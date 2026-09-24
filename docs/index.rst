..
   Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

   This is the MACE documentation master file.

MACE: A Multicore Agentic Co-Design Engine
==========================================

MACE is an agentic AI system that plans, builds, and verifies multicore OpenPiton hardware on the CHIA framework.
It turns a hardware objective into a set of tasks, runs them in parallel, and accepts a change only when Verilator simulation of the RTL passes.
MACE reaches OpenPiton through ``chia_openpiton``, a CHIA adapter for OpenPiton's configure, build, run, and collect operations.

Organization of this Document
-----------------------------

The documentation has six parts.

The :doc:`MACE User Manual <01_mace_user/index>` covers installing MACE, running the loop and the baselines, and reading the results.

The :doc:`MACE Requirements Specification <02_mace_requirements/mace_requirements_specification>` lists what MACE must do.

The :doc:`MACE Design Document <03_mace_design/index>` describes the MACE loop: planning, parallel task execution, integration, verification, and failure analysis.

The :doc:`chia_openpiton Adapter <04_chia_openpiton/index>` describes how the loop builds and runs OpenPiton.

The :doc:`Supported Cores <05_mace_cores/index>` part covers MACE's three OpenPiton cores (Ariane, OpenSPARC T1, and PicoRV32) and how to add another.

The :doc:`MACE Evaluation <06_mace_evaluation/index>` part collects the baselines, results, and cost measurements.

.. toctree::
   :maxdepth: 2
   :hidden:

   01_mace_user/index.rst
   02_mace_requirements/mace_requirements_specification
   03_mace_design/index.rst
   04_chia_openpiton/index.rst
   05_mace_cores/index.rst
   06_mace_evaluation/index.rst
