"""mace.eval.source_faults -- a bug planted in a checkout's design files.

A suite task with a ``source_fault`` runs on checkouts that carry it:
:func:`faulted` writes the text replacements into every checkout before a
job and restores the files after it, however the job ends. A fault that a
method can repair only by editing RTL separates the methods that can edit
RTL from those that cannot (see ``LoopOptions.rtl_edits``).

The originals live in ``build/.mace_fault_backup`` of each checkout, apart
from the RTL edit tool's own backup, so an ``rtl`` task's edits and this
fault never undo each other. A backup an interrupted job left behind is
restored before the next fault is applied.
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

FAULT_BACKUP_DIR = os.path.join("build", ".mace_fault_backup")
_MANIFEST = "manifest.json"
# Faults may touch only the design, as an rtl task may.
FAULT_ROOT = "piton/design"


@dataclass(frozen=True)
class SourceEdit:
    """Replace *old*, which occurs exactly once in *path*, with *new*."""

    path: str
    old: str
    new: str

    def __post_init__(self) -> None:
        rel = os.path.normpath(self.path).replace(os.sep, "/")
        if rel != self.path or not rel.startswith(FAULT_ROOT + "/"):
            raise ValueError(f"fault path must be a normalized path under {FAULT_ROOT}/, got {self.path!r}")
        if not self.old or self.old == self.new:
            raise ValueError(f"fault in {self.path}: old must be non-empty and differ from new")


def restore(root: str) -> list[str]:
    """Restore the files a fault changed in *root*; returns their paths."""
    backup_dir = Path(root) / FAULT_BACKUP_DIR
    manifest_path = backup_dir / _MANIFEST
    if not manifest_path.is_file():
        shutil.rmtree(backup_dir, ignore_errors=True)
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relpath, name in manifest.items():
        (Path(root) / relpath).write_bytes((backup_dir / name).read_bytes())
    shutil.rmtree(backup_dir)
    return sorted(manifest)


def apply(root: str, edits: tuple[SourceEdit, ...]) -> None:
    """Write *edits* into *root*, after restoring any earlier fault.

    Raises ``ValueError`` when an edit's text is not in its file exactly
    once, with every file restored.
    """
    restore(root)
    backup_dir = Path(root) / FAULT_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    try:
        for index, edit in enumerate(edits):
            path = Path(root) / edit.path
            original = path.read_bytes()
            text = original.decode("utf-8", "surrogateescape")
            if text.count(edit.old) != 1:
                raise ValueError(
                    f"fault text occurs {text.count(edit.old)} times in {edit.path} of {root!r}; it must occur once"
                )
            name = f"{index}.orig"
            (backup_dir / name).write_bytes(original)
            # Recorded before the file changes, so a crash still leaves it restorable.
            manifest[edit.path] = name
            (backup_dir / _MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            path.write_bytes(text.replace(edit.old, edit.new, 1).encode("utf-8", "surrogateescape"))
    except Exception:
        restore(root)
        raise


@contextmanager
def faulted(roots: tuple[str, ...], edits: tuple[SourceEdit, ...]):
    """Hold *edits* applied to every checkout in *roots* for the block."""
    applied: list[str] = []
    try:
        for root in roots:
            apply(root, edits)
            applied.append(root)
        yield
    finally:
        for root in applied:
            restore(root)
