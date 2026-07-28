"""Readiness probing — the one place "is it actually serving" is decided.

Shared by every driver, because the mistake it prevents is universal: an engine binds
its socket, or a service manager reports "active", well before the model is resident —
and if the model's warm-up caught an out-of-memory error and carried on, that state is
permanent and invisible to any liveness check. Observed in the field lasting six days.

So a probe answers with three values, and the difference between two of them is the
whole point:

    True  — resident and serving
    False — reachable, but it says it is NOT ready (this is the dangerous case:
            the port is open, so everything else thinks it is fine)
    None  — unreachable or not configured; unknown, never softened to True

Declared per model:

    readiness:
      url: "http://127.0.0.1:PORT/health"
      require: [ok, stt_ready]     # every key must be truthy in the JSON body
      expect: http_ok              # http_ok | models_contains
      model: "org/name"            # for models_contains; the id that must be listed
      timeout_s: 5
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def probe_ready(readiness: dict | None) -> bool | None:
    """Evaluate a declared readiness block. None = unknown/not configured."""
    r = readiness or {}
    url = str(r.get("url") or "").strip()
    if not url:
        return None
    timeout = _f(r.get("timeout_s"), 5.0)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if not (200 <= getattr(resp, "status", 200) < 300):
                return False
            raw = resp.read(65536)
    except (urllib.error.URLError, OSError, ValueError):
        return None  # unreachable is UNKNOWN, not "not ready" and not "ready"

    require = r.get("require") or []
    expect = str(r.get("expect") or "").strip().lower()
    if not require and expect in ("", "http_ok"):
        return True

    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError):
        # A non-JSON 2xx satisfies http_ok but cannot satisfy a key requirement.
        return None if require or expect else True

    if expect == "models_contains":
        want = str(r.get("model") or "").strip()
        ids = [str(d.get("id", "")) for d in (body.get("data") or [])
               if isinstance(d, dict)]
        if not want:
            return bool(ids)
        return any(want == i or want in i for i in ids)

    if isinstance(require, str):
        require = [require]
    for key in require:
        if not _dig(body, str(key)):
            return False
    return True


def _dig(obj, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _f(v, default: float) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default
