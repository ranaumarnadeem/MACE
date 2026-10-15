"""A MACE run on the generic tile: its core descriptor's checks and files.

A spec with ``core="generic"`` names a core descriptor
(:mod:`chia_openpiton.core_descriptor`). Before its first LLM call a run
checks that the generic tile supports the core and that the checkout holds
the files the descriptor names (:func:`unsupported_reasons`); a run that
fails the check ends with status ``"unsupported_core"`` and no build. Before
its first build the run writes the descriptor's ``Flist.core`` and
``link.ld`` into each local checkout (:func:`prepare_checkouts`), and every
build's configuration carries the descriptor's ISA flags and digest
(:func:`config_fields`). A task passes only when every tile's core made its
first request at the descriptor's reset vector (:func:`boot_failures`): a
gate program can pass from the wrong start address.
"""

from __future__ import annotations

import os

import yaml

from chia_openpiton.core_descriptor import (
    SKELETON_MARK,
    CoreDescriptor,
    apply_descriptor,
    boot_problems,
    load_descriptor,
    missing_files,
    support_problems,
)
from chia_openpiton.state_def import PitonRunResult
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
        missing = missing_files(desc, checkout, wrapper=False)
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


def boot_failures(spec: MaceSpec, runs: tuple[PitonRunResult, ...], num_tiles: int) -> tuple[str, ...]:
    """Ladder step 2 on a task's first simulation that ran: the tiles whose
    core did not start at the descriptor's reset vector. Empty when they all
    did, when nothing ran, and for a core other than "generic"."""
    desc = descriptor(spec)
    ran = [r for r in runs if r.run_dir]
    if desc is None or not ran:
        return ()
    return boot_problems(desc, ran[0].first_requests, num_tiles)


def inputs_brief(spec: MaceSpec) -> str:
    """The core's lines in every LLM method's inputs (see
    :func:`mace.planner.render_inputs`); "" for a core other than "generic"."""
    desc = descriptor(spec)
    if desc is None:
        return ""
    return (
        f"Core: {desc.core} on OpenPiton's generic tile: top module {desc.top_module}, ISA {desc.march}, "
        f"{desc.bus} bus, reset vector {desc.reset_vector:#010x}. It connects to the tile through "
        f"piton/design/chip/tile/{desc.wrapper}, a module named generic_core_wrapper with fixed ports. "
        f"While that file still holds its '{SKELETON_MARK}' block, no core runs, and an rtl task must write "
        "the block first.\n"
    )


def wrapper_brief(spec: MaceSpec) -> str:
    """What an rtl task's agent is told about writing the core's wrapper;
    "" for a core other than "generic"."""
    desc = descriptor(spec)
    if desc is None:
        return ""
    with open(spec.core_descriptor) as f:
        descriptor_text = f.read().strip()
    bridge = {
        "simple": "The core's memory interface is the simple bus itself, so connect it without a bridge.",
        "obi": "Instantiate generic_bridge_obi (piton/design/chip/tile/generic/rtl/generic_bridge_obi.v) "
               "between the core's instruction and data ports and the simple bus.",
        "wishbone": "Instantiate generic_bridge_wishbone (piton/design/chip/tile/generic/rtl/"
                    "generic_bridge_wishbone.v) between the core's Wishbone master port and the simple bus.",
    }[desc.bus]
    return "\n".join((
        f"The core's wrapper is piton/design/chip/tile/{desc.wrapper}. Replace its '{SKELETON_MARK}' block, "
        f"from the comment that opens it to the one that closes it, with an instance of {desc.top_module} and "
        "its bridge, keeping the module's ports as they are.",
        bridge,
        "Read the core's top module for its ports and parameters. The simple bus rules are at the top of "
        "piton/design/chip/tile/generic/rtl/generic_l15_transducer.v, and PicoRV32's wrapper, "
        "piton/design/chip/tile/generic/cores/picorv32/generic_core_wrapper.v, is a finished example.",
        f"Set the core to start at {desc.reset_vector:#010x}; a core that starts elsewhere fails the run even "
        "if its programs pass. Tie off its interrupt, debug, and test inputs, and drive any enable input "
        "that lets it fetch. hart_id carries the tile's ID for a core with a hart ID input.",
        "The core descriptor:",
        descriptor_text,
    ))
