"""Check the docs site and the READMEs against the writing rules in
.claude/claude_docs.md that a script can check.

Run:
    python scripts/check_docs_style.py

Every docs page starts with the copyright line, a blank line, and its title,
and every .rst page carries the copyright line. No checked file has an em dash
or a section sign, or, outside code, a word the rules leave out. The
standalone notes docs/README.md and docs/TECHNICAL_GUIDE.md are not part of
the site and are not checked.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
COPYRIGHT = "Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran"

# "key" in the sense of important is also left out, but only a reader can
# tell that sense apart, so it is not checked here.
BANNED = (
    "real", "actual", "actually", "genuine", "genuinely", "honest", "honestly",
    "truly", "leverage", "utilize", "seamless", "robust", "comprehensive",
    "crucial", "delve", "showcase", "moreover", "furthermore", "additionally",
    "overall", "various", "numerous", "in order to", "note that",
    "it is worth noting", "importantly", "state-of-the-art",
)
BANNED_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in BANNED) + r")\b", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN_RE = re.compile(r"`+[^`]*`+")
LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")


def md_pages() -> list[Path]:
    return sorted(p for p in DOCS.glob("0*/**/*.md"))


def rst_pages() -> list[Path]:
    return [DOCS / "index.rst", *sorted(DOCS.glob("0*/**/*.rst"))]


def checked_files() -> list[Path]:
    return [*md_pages(), *rst_pages(), DOCS / "llms.txt",
            ROOT / "README.md", ROOT / "chia_openpiton" / "README.md"]


def prose_lines(lines: list[str]):
    """(line number, text) for each line outside a fenced block, with code
    spans and link targets removed."""
    in_fence = False
    for number, line in enumerate(lines, start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield number, LINK_TARGET_RE.sub("](", CODE_SPAN_RE.sub("", line))


def problems(path: Path) -> list[str]:
    rel = path.relative_to(ROOT).as_posix()
    lines = path.read_text(encoding="utf-8").splitlines()
    found = []
    if path.suffix == ".md" and path.is_relative_to(DOCS):
        if lines[:1] != [f"% {COPYRIGHT}"] or lines[1:2] != [""] or not lines[2:3] or not lines[2].startswith("# "):
            found.append(f"{rel}:1: a page starts with '% {COPYRIGHT}', a blank line, and '# Title'")
    if path.suffix == ".rst" and COPYRIGHT not in "\n".join(lines):
        found.append(f"{rel}:1: missing the copyright line")
    for number, line in enumerate(lines, start=1):
        if "—" in line:
            found.append(f"{rel}:{number}: em dash")
        if "§" in line:
            found.append(f"{rel}:{number}: section sign; write 'Section N'")
    for number, line in prose_lines(lines):
        for match in BANNED_RE.finditer(line):
            found.append(f"{rel}:{number}: leave out '{match.group(0)}'")
    return found


def main() -> int:
    found = [p for path in checked_files() for p in problems(path)]
    for problem in found:
        print(problem)
    print(f"{len(checked_files())} files checked, {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
