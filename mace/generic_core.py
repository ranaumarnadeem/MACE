"""A MACE run on the generic tile: its core descriptor's checks and files.

A spec with ``core="generic"`` names a core descriptor
(:mod:`chia_openpiton.core_descriptor`). Before its first LLM call a run
checks that the generic tile supports the core and that the checkout holds
the files the descriptor names (:func:`unsupported_reasons`); a run that
fails the check ends with status ``"unsupported_core"`` and no build. Before
its first build the run writes the descriptor's ``Flist.core`` and
``link.ld`` into each local checkout (:func:`prepare_checkouts`), and every
build's configuration carries the descriptor's ISA flags and digest
(:func:`config_fields`).
"""

from __future__ import annotations

import os

import yaml

from chia_openpiton.core_descriptor import (
    CoreDescriptor,
    apply_descriptor,
    load_descriptor,
    missing_files,
    support_problems,
)
from mace.spec import MaceSpec


def descriptor(spec: MaceSpec) -> CoreDescriptor | None:
    """The spec's core descriptor, or None for a core other than "generic"."""
    return load_descriptor(spec.core_descriptor) if spec.core == "generic" else None


def unsupported_reasons(spec: MaceSpec, checkout: str | None) -> tuple[str, ...]:
    """Why the spec's core cannot run, checked before any build; empty when
    it can, and for every core other than "generic". *checkout*, when given,
    is checked for the files the descriptor names."""
    if spec.core != "generic":
        return ()
    try:
        desc = load_descriptor(spec.core_descriptor)
    except (OSError, ValueError, yaml.YAMLError) as e:
        return (f"core descriptor {spec.core_descriptor}: {e}",)
    problems = list(support_problems(desc))
    if checkout:
        missing = missing_files(desc, checkout)
        if missing:
            problems.append(
                "files the core descriptor names are not under piton/design/chip/tile/: " + ", ".join(missing)
            )
    return tuple(problems)


def prepare_checkouts(spec: MaceSpec, piton_roots: tuple[str, ...]) -> None:
    """Write the descriptor's Flist.core and link.ld into each checkout on
    this machine. A no-op for a core other than "generic"."""
    desc = descriptor(spec)
    if desc is None:
        return
    for root in piton_roots:
        if os.path.isdir(root):
            apply_descriptor(desc, root)


def config_fields(spec: MaceSpec) -> tuple[tuple[str, ...], str]:
    """(extra sims flags, PitonConfig.generic_core) for the spec's core:
    the descriptor's ISA flags and "<name>@<digest>", or ``((), "")`` for a
    core other than "generic"."""
    desc = descriptor(spec)
    if desc is None:
        return (), ""
    return desc.sims_flags(), f"{desc.core}@{desc.digest}"
