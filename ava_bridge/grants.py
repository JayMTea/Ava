"""Durable JIT-consent grants for connector actions.

The consent model (docs/dev/CONNECTOR_DISCOVERY_UX_PLAN.md): connecting an app
never shows an endpoint review. Actions are gated by access tier at call time —
`read` runs silently, `write` asks on FIRST use, `destructive` asks every time.
When the operator answers a write prompt with "Always allow", the decision is
remembered here, so the question is asked exactly once per (connector, action).

Grants are user data, not source: they live at $AVA_HOME/connector_grants.yaml
(same rule as ava.yaml — a fork ships none). Shape:

    persona:
      post_persona: {granted: "2026-07-08T20:41:00Z", by: owner}

Revoking is deleting the entry (the connector settings page, or this API).
Grant/revoke are audit events, so "what can this app do and since when" is
always answerable from the ledger.
"""
from __future__ import annotations

import os
import threading
import time

from . import settings

try:
    import yaml
except Exception:  # noqa: BLE001 — PyYAML is a core dep; tolerate for tooling
    yaml = None

PATH = settings.home("connector_grants.yaml")

_lock = threading.Lock()
_cache: dict = {"data": None, "mtime": 0.0}


def _load() -> dict:
    """The grants mapping {cid: {action: meta}}, cached on file mtime."""
    if yaml is None:
        return {}
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        _cache.update(data={}, mtime=0.0)
        return {}
    if _cache["data"] is not None and _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        with open(PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 — a corrupt file must not break the gate
        data = {}
    if not isinstance(data, dict):
        data = {}
    _cache.update(data=data, mtime=mtime)
    return data


def _save(data: dict) -> None:
    """Atomic write (tmp + rename) so a crash never leaves a half-file."""
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=True)
    os.replace(tmp, PATH)
    _cache.update(data=data, mtime=os.path.getmtime(PATH))


def has(cid: str, action: str) -> bool:
    with _lock:
        return action in (_load().get(cid) or {})


def grant(cid: str, action: str, by: str = "owner") -> None:
    """Persist a standing "always allow" for one connector action.

    Durable (written to disk, survives restart) and idempotent — granting twice
    just refreshes the timestamp. `by` is recorded in the audit ledger as the actor
    and is free text; "owner" means a human clicked Always allow in the approvals
    banner. There is no expiry: a grant lives until revoke() removes it or the
    connector is deleted.
    """
    from . import audit
    with _lock:
        data = {k: dict(v) for k, v in _load().items()}
        data.setdefault(cid, {})[action] = {
            "granted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "by": by,
        }
        _save(data)
    audit.record("grant", connector=cid, action=action, actor=by)


def revoke(cid: str, action: str) -> bool:
    """Remove a standing grant. Returns False if there was nothing to remove.

    The bool is the point: a caller reporting "revoked" on a False would be lying
    to the owner about a permission that was never there. Prunes the connector's
    entry entirely once its last action is gone, so `has()` cannot be fooled by an
    empty dict left behind.
    """
    from . import audit
    with _lock:
        data = {k: dict(v) for k, v in _load().items()}
        if action not in (data.get(cid) or {}):
            return False
        del data[cid][action]
        if not data[cid]:
            del data[cid]
        _save(data)
    audit.record("revoke", connector=cid, action=action)
    return True


def forget(cid: str) -> list[str]:
    """Drop every standing grant for one connector. Returns the actions removed.

    `grant()` above has always promised that "a grant lives until revoke()
    removes it **or the connector is deleted**", and nothing honoured the second
    half. This file is keyed on the connector id, and ids are reusable — so
    deleting an app and adding another one under the same name silently
    re-inherited every prior "Always allow". A permission the owner granted to
    one app, restored for a different one, with no prompt.

    Audited as a revoke per action rather than one bulk row: the ledger has to be
    able to answer "when did Ava stop being allowed to do X", and a single
    "deleted the connector" line cannot.
    """
    from . import audit
    with _lock:
        data = {k: dict(v) for k, v in _load().items()}
        actions = sorted(data.get(cid) or {})
        if not actions:
            return []
        del data[cid]
        _save(data)
    for action in actions:
        audit.record("revoke", connector=cid, action=action,
                     reason="connector removed")
    return actions


def for_connector(cid: str) -> dict:
    """{action: meta} for one connector (the settings page's grant column)."""
    with _lock:
        return dict(_load().get(cid) or {})
