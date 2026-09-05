"""chia_openpiton — CHIA platform support for OpenPiton.

Wraps OpenPiton's ``sims`` tool as CHIA nodes so agentic loops can configure,
build, simulate, run workloads against and collect results from an OpenPiton
manycore through one interface.

This package deliberately imports nothing from ``mace``: it is meant to be
dropped into an upstream CHIA checkout as ``chia/openpiton/`` unchanged.

``state_def`` and ``parse`` are dependency-free and always importable, so
parsers and config types can be used (and tested) without Ray or MCP present.
"""

from chia_openpiton.state_def import (
    DEFAULT_CACHES,
    MAX_TILES_PER_AXIS,
    NetworkConfig,
    PitonBuildArtifact,
    PitonCollectResult,
    PitonConfig,
    PitonCore,
    PitonRegressResult,
    PitonRunResult,
    SimType,
    Verdict,
)

__all__ = [
    "DEFAULT_CACHES",
    "MAX_TILES_PER_AXIS",
    "NetworkConfig",
    "PitonBuildArtifact",
    "PitonCollectResult",
    "PitonConfig",
    "PitonCore",
    "PitonRegressResult",
    "PitonRunResult",
    "SimType",
    "Verdict",
]

try:
    from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode

    __all__.append("OpenPitonWorkspaceNode")
except ImportError:  # pragma: no cover - ray / chia not installed
    pass
