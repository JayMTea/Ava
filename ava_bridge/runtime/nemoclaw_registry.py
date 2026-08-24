"""NemoClaw's own sandbox registry — one reader, two callers.

`~/.nemoclaw/sandboxes.json` is the single best provisioning-assert source on
the box: it carries `customPolicies[]` with the full applied content (so a
policy can be diffed byte-for-byte with no CLI call), plus `imageTag`, `model`,
`provider`, the recorded gateway port and the nemoclaw/openshell/agent versions.
Crucially it is readable with the container STOPPED — which is exactly when you
most want to know what is live.

This lives in its own module because BOTH the CLI adapter and the gateway
adapter need it and neither owns it. Copy-pasting it would not trip
`tests/test_module_boundaries.py` — the import would be perfectly public — but a
second reader of one file is a second answer to one question, free to drift.
"""
from __future__ import annotations

import json
import os
import time

_cache: dict = {"ts": 0.0, "records": None}
_CACHE_S = 30.0


def _home() -> str:
    return os.environ.get("NEMOCLAW_HOME") or os.path.expanduser("~/.nemoclaw")


def _all(max_age: float = _CACHE_S) -> dict:
    """Every sandbox NemoClaw knows about. Never raises: a renamed file or a
    NemoClaw format change must degrade to `unknown`, not break the panel."""
    now = time.time()
    if _cache["records"] is not None and (now - _cache["ts"]) < max_age:
        return _cache["records"]
    records: dict = {}
    try:
        with open(os.path.join(_home(), "sandboxes.json"), encoding="utf-8") as f:
            data = json.load(f)
        got = data.get("sandboxes")
        if isinstance(got, dict):
            records = got
    except Exception:  # noqa: BLE001 — absent registry is a normal state
        records = {}
    _cache.update(ts=now, records=records)
    return records


def registry_record(sandbox: str | None = None,
                    max_age: float = _CACHE_S) -> dict | None:
    """This sandbox's entry, or None when the registry cannot answer.

    `None` means "we could not look", which `provision.item_state` renders as
    `unknown` rather than as `undeployed`. An empty dict would read as "the
    registry says there is nothing", which is a different and wrong claim.
    """
    if sandbox is None:
        from .. import config
        sandbox = config.OC_SANDBOX
    entry = _all(max_age).get(sandbox)
    return entry if isinstance(entry, dict) else None


def openclaw_gateway_port(sandbox: str | None = None) -> int | None:
    """The port OpenClaw's own gateway (and its Control UI) is forwarded to.

    THIS IS `dashboardPort`, NOT `gatewayPort`, and the distinction is a
    credential boundary rather than a detail. A real record on a working box:

        dashboardPort  18789     <- OpenClaw's gateway: JSON-RPC + Control UI
        gatewayPort    8080      <- OpenShell's gateway: mTLS, a different
                                    service with a different trust model

    `agent.gateway.*` mints an `operator.admin` bearer for OpenClaw. Sending it
    to `gatewayPort` would hand an admin token to the wrong daemon, which
    authenticates with client certificates and has no idea what to do with it.
    `nemoclaw <name> rebuild` can move the dashboard port (and `connect` re-
    resolves it), which is why this is read rather than hardcoded.
    """
    rec = registry_record(sandbox) or {}
    for key in ("dashboardPort", "dashboard_port"):
        val = rec.get(key)
        if isinstance(val, int) and val > 0:
            return val
    return None


def openshell_gateway_port(sandbox: str | None = None) -> int | None:
    """OpenShell's mTLS gateway port — the OTHER one. Named so that the two can
    never be reached for interchangeably; see the warning above."""
    rec = registry_record(sandbox) or {}
    val = rec.get("gatewayPort")
    return val if isinstance(val, int) and val > 0 else None


def invalidate() -> None:
    _cache.update(ts=0.0, records=None)
