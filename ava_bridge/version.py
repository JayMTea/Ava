"""Ava version — single source of truth.

Resolution (highest wins):
    1. AVA_VERSION env  — injected by the release CI from the git tag (vX.Y.Z),
       and baked into the Docker image as a build arg.
    2. the committed VERSION file at the repo root.
    3. a dev fallback ("0.0.0+dev") when neither is present.

Import `__version__` / `version()` anywhere a version is shown (the CLI,
/api/health, /api/brand) so there is exactly one place the number comes from.
"""
from __future__ import annotations

import os
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _from_file() -> str | None:
    try:
        v = _VERSION_FILE.read_text(encoding="utf-8").strip()
        return v or None
    except OSError:
        return None


def version() -> str:
    """The resolved version string, without a leading 'v' (tags are `vX.Y.Z`)."""
    v = os.environ.get("AVA_VERSION") or _from_file() or "0.0.0+dev"
    return v.lstrip("v").strip()


def revision() -> str | None:
    """The build's git revision, if known (CI injects AVA_REVISION = the SHA)."""
    rev = os.environ.get("AVA_REVISION")
    return rev.strip()[:12] if rev else None


__version__ = version()
