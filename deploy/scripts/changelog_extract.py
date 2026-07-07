#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a given version, for GitHub Release notes.

Usage:  python3 deploy/scripts/changelog_extract.py 0.1.0
Looks for a `## [0.1.0]` (or `## [0.1.0] — date`) heading and prints its body up
to the next `## ` heading. Falls back to the `[Unreleased]` section if the exact
version heading isn't present yet (so a tag cut before the changelog rename still
produces sensible notes). Exits 0 always; prints a pointer if nothing is found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"


def _section(lines: list[str], key: str) -> list[str] | None:
    # Match `## [key]` with optional trailing "— date" etc.
    head = re.compile(r"^##\s*\[" + re.escape(key) + r"\]")
    out: list[str] = []
    grabbing = False
    for line in lines:
        if grabbing:
            if line.startswith("## "):
                break
            out.append(line)
        elif head.match(line):
            grabbing = True
    if not grabbing:
        return None
    # Trim leading/trailing blank lines.
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def main() -> int:
    version = (sys.argv[1] if len(sys.argv) > 1 else "").lstrip("v").strip()
    try:
        lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        print("See CHANGELOG.md for details.")
        return 0
    body = None
    if version:
        body = _section(lines, version)
    if body is None:
        body = _section(lines, "Unreleased")
    if not body:
        print(f"Release {version or ''}. See CHANGELOG.md for details.".strip())
        return 0
    print("\n".join(body))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
