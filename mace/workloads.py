"""mace.workloads -- frozen gate C programs and their integrity check.

These .c files are the loop's pass/fail oracle (see mace.spec.MaceSpec.
workloads): whether the design under test still "works" is decided entirely
by whether building and running one comes back with verdict == "pass". An
agent that could edit its own oracle could make every task look like it
passed, so these are checksummed and meant to be verified before a run
starts -- the same reasoning any CI's test-integrity check is for.

Each program is real, buildable RISC-V C using OpenPiton's own testbench
conventions (argv[0][0]/argv[0][1] for hart id/count; see an OpenPiton
checkout's piton/verif/diag/assembly/include/riscv/ariane/util.h), not
pseudocode -- sims compiles them itself via -asm_diag_root pointed at this
directory, and all three are proven passing on real Ariane hardware (1x1;
multi-tile is untested pending GCP credits -- see the top-level README).

Every shared value here is written with ATOMIC_OP and read back with
ATOMIC_FETCH_OP, never a plain load or store of anything another hart (or,
empirically, even the same hart through an array element) might have just
written: measured on real hardware, plain volatile access to an array
element was not reliably visible across a write and a same-hart read a few
instructions later, though the same value through ATOMIC_FETCH_OP always
was. A plain *scalar* (non-array) variable accessed the same way was fine,
but rather than trust which cases happen to work, every workload here goes
through the atomic path uniformly. Each also needs a larger-than-default
``rtl_timeout`` -- see RECOMMENDED_RTL_TIMEOUT.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

WORKLOADS_DIR = Path(__file__).parent / "workloads"
CHECKSUMS_FILE = WORKLOADS_DIR / "CHECKSUMS"

# Measured on real 1x1 Ariane hardware: the adapter's default (sims' own
# default, 50000 cycles) was enough for barrier_atomic.c and
# scatter_gather.c but cut producer_consumer.c off mid-printf. 1000000
# matches what OpenPiton's own ariane_tile1_simple regression group already
# uses for hello_world.c/accu_test.c/amo_align.c on the same mesh size, so
# it is the one value proven to be enough for everything in this directory.
# Scale up for a larger mesh, the same way that group's own multi-tile
# sibling groups do.
RECOMMENDED_RTL_TIMEOUT = 1_000_000


def compute_checksums(workloads_dir: Path = WORKLOADS_DIR) -> dict[str, str]:
    """sha256 of every ``*.c`` file in *workloads_dir*, by filename."""
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(workloads_dir.glob("*.c"))
    }


def read_checksums(checksums_file: Path = CHECKSUMS_FILE) -> dict[str, str]:
    """Parse a ``sha256sum``-format CHECKSUMS file into ``{filename: hexdigest}``.

    Tolerates both the text-mode (two spaces) and binary-mode (space then
    ``*``) separators ``sha256sum`` can emit, so ``sha256sum -c CHECKSUMS``
    also works as an independent, non-Python check of the same file.
    """
    checksums: dict[str, str] = {}
    for line in checksums_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, name = line.split(maxsplit=1)
        checksums[name.strip().lstrip("*")] = digest.strip()
    return checksums


def verify_checksums(
    workloads_dir: Path = WORKLOADS_DIR, checksums_file: Path = CHECKSUMS_FILE
) -> None:
    """Raise if any ``*.c`` file's content doesn't match CHECKSUMS.

    Also raises if a file is missing from CHECKSUMS or CHECKSUMS names a
    file that isn't there -- either one means the two have drifted, which
    is exactly the state this check exists to catch.

    Raises:
        ValueError: On a checksum mismatch or a set-of-files mismatch.
    """
    actual = compute_checksums(workloads_dir)
    expected = read_checksums(checksums_file)
    if actual.keys() != expected.keys():
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        raise ValueError(
            f"workloads and CHECKSUMS disagree on which files exist "
            f"(missing: {missing}, unexpected: {extra})"
        )
    mismatched = sorted(name for name in actual if actual[name] != expected[name])
    if mismatched:
        raise ValueError(f"checksum mismatch for: {mismatched}")
