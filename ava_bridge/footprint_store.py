"""What models have actually cost on THIS box, remembered between runs.

The point of the trust ladder in `model_footprint` is that the top rung is a
measurement — but it is only reachable while the model is loaded. Ollama evicts
after ~5 minutes idle, so an owner browsing models half an hour later gets a
derived estimate for a model this machine has already run and measured. That is
the one situation where Ava knows the true answer and reports a guess.

So: when a model is observed resident, write down what it took. Next time it is
the top rung again, and the box gets more accurate the more it is used rather
than less.

**Only measurements are stored.** Never a derived or declared figure — the file
would then be a cache of guesses that outlive their inputs, and a stale guess is
harder to distrust than a fresh one. Re-derivation is cheap; a remembered lie is
not.

Same shape as the rest of `AVA_HOME` state: one small JSON file, written
atomically, and a corrupt or unreadable one degrades to "we remember nothing"
rather than taking a model list down with it.
"""
from __future__ import annotations

import json
import os
import tempfile

from .model_footprint import OBSERVED, Footprint

_FILE = "model_footprints.json"
# A record older than this describes a machine that may have changed underneath
# it — a raised WSL2 ceiling, a different GPU, a new quantisation of the same
# tag. Re-observing costs one API call; trusting a stale number costs a wrong
# recommendation that looks authoritative.
_MAX_AGE_S = 60 * 60 * 24 * 30
_MAX_ENTRIES = 200


def _path() -> str:
    from . import settings
    return os.path.join(settings.data_dir(), _FILE)


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001 — absent, corrupt, or unreadable
        return {}


def get(model: str, now: float | None = None) -> Footprint | None:
    """What this box measured for `model`, or None if it never has."""
    row = _load().get((model or "").strip())
    if not isinstance(row, dict):
        return None
    try:
        weight = float(row["weight_gb"])
        seen = float(row.get("seen", 0))
    except (KeyError, TypeError, ValueError):
        return None
    import time
    if weight <= 0 or (now or time.time()) - seen > _MAX_AGE_S:
        return None
    spilled = row.get("spilled")
    return Footprint(
        model=model, source=OBSERVED, weight_gb=weight,
        spilled=spilled if isinstance(spilled, bool) else None,
        detail="measured on this machine the last time it ran")


def remember(fp: Footprint, now: float | None = None) -> bool:
    """Record a MEASURED footprint. Returns whether anything was written.

    Refuses anything but an observation, on purpose: see the module docstring.
    """
    if fp.source != OBSERVED or not fp.model or not fp.weight_gb:
        return False
    import time
    data = _load()
    data[fp.model] = {"weight_gb": round(fp.weight_gb, 2),
                      "spilled": fp.spilled,
                      "seen": now or time.time()}
    if len(data) > _MAX_ENTRIES:
        # Oldest out. An unbounded file on a box someone is genuinely exploring
        # models on is a slow leak nobody would ever look for.
        for k, _ in sorted(data.items(),
                           key=lambda kv: kv[1].get("seen", 0))[:len(data) - _MAX_ENTRIES]:
            data.pop(k, None)
    return _write(data)


def observe_and_remember(base_url: str, engine: str = "") -> int:
    """Ask the engine what it is holding and record all of it. Returns the count.

    Cheap enough to call opportunistically from anywhere that already talks to
    the engine — one request, and it only writes when something was resident.
    """
    from . import model_footprint
    try:
        found = model_footprint.observed(base_url, engine)
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for fp in found.values() if remember(fp))


def _write(data: dict) -> bool:
    """Atomic replace, so a crash mid-write cannot leave a half file that then
    reads as "we remember nothing" for every model at once."""
    try:
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 — remembering is an optimisation, never a
        # requirement: failing to write must not fail the caller.
        return False
