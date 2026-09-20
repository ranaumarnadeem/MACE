"""mace.cli.spec_file -- parsing for `read_spec`'s plain-text spec files.

Deliberately forgiving, the same fail-open convention mace.agents' footer
parsers use: a spec file can be as simple as a paragraph of plain English
describing the goal (the whole file becomes the objective verbatim), or it
can use a few recognized ``key: value`` lines for finer control. Either way
parsing never raises on malformed input -- an unrecognized line is just not
one of the known keys, not an error.
"""

from __future__ import annotations

import re

_KEY_LINE = re.compile(r"(?im)^\s*(objective|workloads|core)\s*:\s*(.+)$")


def parse_spec_file(text: str) -> dict:
    """*text* -> a dict of overrides: ``objective`` (str), ``workloads``
    (tuple[str, ...]), ``core`` (str) -- only the keys actually found.

    If no ``objective:`` line is present, the whole file's stripped text
    becomes the objective (the common case: a spec file that's just a plain
    description of what to verify, not a structured format).
    """
    overrides: dict = {}
    for match in _KEY_LINE.finditer(text):
        key, value = match.group(1).lower(), match.group(2).strip()
        if key == "workloads":
            overrides["workloads"] = tuple(
                w.strip() for w in value.split(",") if w.strip()
            )
        else:
            overrides[key] = value
    if not overrides:
        # Only when NOTHING recognized was found -- a file with just
        # `workloads:`/`core:` lines and no `objective:` is a genuine,
        # reasonable structured spec, not the plain-English case this
        # fallback exists for. Keying this off "objective" missing alone
        # used to fire there too, polluting the LLM prompt with the raw
        # `workloads: ...\ncore: ...` text as a garbled "objective".
        stripped = text.strip()
        if stripped:
            overrides["objective"] = stripped
    return overrides
