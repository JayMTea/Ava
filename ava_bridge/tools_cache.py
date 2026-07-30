"""Access-tier cache for dynamically discovered connector tools.

The ava-tools/1 facade (docs/CONNECTOR_SDK.md §5) lets an app declare a JIT
consent tier per tool (`access: read | write | destructive`). The gate
(`connectors.needs_confirm`) is synchronous and can't fetch /tools per call, so
every successful discovery — the Hub's Detect-and-connect, the agent's
find_tool, Deploy — writes the tiers through to this cache, and the gate reads
them from here.

Fail-ask, never fail-open: a tool that has never been seen (or declares an
invalid tier) stays `write` — it asks the operator on first use. Tiers are
self-reported by the app: they can only make a tool quieter, never extend its
reach (egress policy, destructive-never-grantable, author `confirm:`, and the
audit ledger all still apply on Ava's side).

Storage: $AVA_HOME/connector_tools_cache.json — user data, same rules as
connector_grants.yaml (atomic writes, mtime-cached reads). Shape:

    {"persona": {"list_personas": {"access": "read", "description": "…"}}}
"""
from __future__ import annotations

import json
import os
import threading

from . import settings

PATH = settings.home("connector_tools_cache.json")
# Kept in step with connectors._TIERS. This list was short by two: `physical`
# and (now) `sensitive` were coerced to "write" on the way in and filtered out on
# the way out, so a device tool self-reporting `physical` was stored — and
# enforced — as the weaker `write`. docs/CONNECTOR_SDK.md works around that by
# telling device connectors to always ship `"*": physical` in the manifest, which
# outranks this cache; the cache being unable to represent the tier was still a
# silent downgrade for anyone who didn't.
_TIERS = ("read", "sensitive", "write", "destructive", "physical")

_lock = threading.Lock()
_cache: dict = {"data": None, "mtime": 0.0}


def _load() -> dict:
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        _cache.update(data={}, mtime=0.0)
        return {}
    if _cache["data"] is not None and _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 — a corrupt cache must not break the gate
        data = {}
    if not isinstance(data, dict):
        data = {}
    _cache.update(data=data, mtime=mtime)
    return data


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True, indent=1)
    os.replace(tmp, PATH)
    _cache.update(data=data, mtime=os.path.getmtime(PATH))


def update(cid: str, tools: list) -> None:
    """Write-through from one /tools (or MCP list) result. Replaces the
    connector's entry wholesale — the facade is the source of truth, so tools
    it no longer lists drop out (their grants stay; grants are the operator's)."""
    entry: dict = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        acc = str(t.get("access") or "").lower()
        entry[name[:64]] = {
            "access": acc if acc in _TIERS else "write",
            "description": str(t.get("description") or "")[:200],
        }
    with _lock:
        data = {k: dict(v) for k, v in _load().items()}
        if entry:
            data[cid] = entry
        elif cid in data:
            del data[cid]
        _save(data)


def access(cid: str, name: str) -> str | None:
    """The cached tier for one tool, or None if never seen (caller fail-asks)."""
    with _lock:
        t = (_load().get(cid) or {}).get(name)
    acc = (t or {}).get("access")
    return acc if acc in _TIERS else None


def for_connector(cid: str) -> dict:
    """{name: {access, description}} for the permissions sheet."""
    with _lock:
        return {k: dict(v) for k, v in (_load().get(cid) or {}).items()}
