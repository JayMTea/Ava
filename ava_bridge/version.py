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


# --- is the running process still the code on disk? --------------------------- #
#
# A long-lived process holds the code it imported, not the code in the tree. On
# 2026-08-13 a router had been serving a hardcoded default deleted from the tree
# four days earlier — the constant existed nowhere on disk, `grep` found nothing,
# and every surface that could have named the problem was reading the new code
# while the failure lived in the old. Uptime cannot tell you this: "running for
# four days" and "running the WRONG four-day-old code" look identical.
#
# So: stamp what was loaded, compare it to what is on disk, and let something say
# when they differ. Same shape the SPA already uses for `bridge_outdated`, one
# layer down.
#
# `revision()` alone is not enough and would be actively misleading: a Docker
# image bakes AVA_REVISION at build time, so a `git pull` on a running box leaves
# it truthfully reporting a SHA that is no longer what is on disk. The newest
# source mtime is the reading that actually moves, and it needs no git.

_CODE_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("ava_bridge", "agent")
_SCAN_FILES = ("phone_bridge.py", "ava_router.py", "ava_cli.py", "voice_ava.py")


def _newest_source_mtime() -> float:
    """Newest mtime across the Python that defines this process's behaviour.

    Bounded on purpose: the package dirs and the entrypoints, skipping `.venv`,
    caches and data. This runs at import and once per watchdog cycle, never per
    request.
    """
    newest = 0.0
    try:
        for name in _SCAN_FILES:
            p = _CODE_ROOT / name
            if p.exists():
                newest = max(newest, p.stat().st_mtime)
        for d in _SCAN_DIRS:
            root = _CODE_ROOT / d
            if not root.is_dir():
                continue
            for p in root.rglob("*.py"):
                if "__pycache__" in p.parts or ".venv" in p.parts:
                    continue
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    continue
    except Exception:  # noqa: BLE001 — an unknown stamp is a state, not an error
        return 0.0
    return newest


def _git_head() -> str:
    try:
        import subprocess

        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(_CODE_ROOT),
                           capture_output=True, text=True, timeout=5)
        return p.stdout.strip()[:12] if p.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — not a git checkout, or no git
        return ""


def tree_stamp() -> dict:
    """What the code on disk says, read NOW."""
    return {"revision": revision() or _git_head(),
            "source_mtime": _newest_source_mtime()}


#: Captured at import — i.e. at the moment this process loaded its code. A module
#: constant by construction: it cannot re-read itself later and quietly become
#: current, which is exactly the failure mode being detected.
BOOT_STAMP = tree_stamp()


def boot_stamp() -> dict:
    """What the code this process is RUNNING said, frozen at import."""
    return dict(BOOT_STAMP)


def code_drift() -> dict:
    """Is this process still running the code that is on disk?

    `stale` is only True when both sides are known AND they differ. An unknown
    stamp on either side degrades to "we cannot tell" — never to "stale" — for
    the same reason a residency probe that cannot reach an engine reports unknown
    rather than absent: a guess that looks like a finding is worse than no
    finding.
    """
    boot, tree = boot_stamp(), tree_stamp()
    rev_known = bool(boot.get("revision")) and bool(tree.get("revision"))
    mtime_known = bool(boot.get("source_mtime")) and bool(tree.get("source_mtime"))
    known = rev_known or mtime_known
    stale = bool((rev_known and boot["revision"] != tree["revision"])
                 or (mtime_known and tree["source_mtime"] > boot["source_mtime"]))
    since = (tree["source_mtime"] - boot["source_mtime"]) if stale and mtime_known else 0.0
    return {"stale": stale, "known": known, "since_s": max(0.0, since),
            "boot": boot, "tree": tree}


__version__ = version()
