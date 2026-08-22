"""Shared security helpers for host callback routes and proxy surfaces.

Security-sensitive code should centralize policy decisions here instead of
open-coding allowlists in individual route handlers. New host capabilities get
one named internal scope and one narrow route check by default.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
from typing import Iterable
from urllib.parse import urlparse


def constant_time_equals(presented: str | bytes, expected: str | bytes) -> bool:
    """Constant-time equality that tolerates non-ASCII input.

    `hmac.compare_digest` raises TypeError on a `str` holding any non-ASCII
    character, so comparing secrets as text made an accented character an
    unhandled 500: a permanent lockout on /login (the owner cannot reach the
    page that would let them change it) and, worse, an unauthenticated way for
    any caller to force an error on /internal by sending a non-ASCII bearer
    token. Compare the bytes the caller actually sent instead.

    `surrogatepass` matters: header and form decoding can hand us a lone
    surrogate, and a plain .encode("utf-8") would raise UnicodeEncodeError —
    reintroducing the same 500 through a different door.
    """
    if isinstance(presented, str):
        presented = presented.encode("utf-8", "surrogatepass")
    if isinstance(expected, str):
        expected = expected.encode("utf-8", "surrogatepass")
    return hmac.compare_digest(presented, expected)


def secure_opener(path: str, flags: int) -> int:
    """open() opener that creates new files 0600, so a secret never briefly
    exists world-readable between create and chmod.

    Pass as `open(p, "w", opener=secure_opener)`. Write-then-chmod is not
    equivalent: a crash in between leaves the file 0644 permanently.
    """
    return os.open(path, flags, 0o600)


# What each MCP capability group may reach on /internal/*. Enforced by
# ava_bridge/internal.group_may(), which ava_bridge/auth.auth_gate calls for every
# /internal request.
#
# These sets are derived from what each agent/mcp_server_<group>/ ACTUALLY calls,
# not from what sounds tidy. Getting that wrong is why enforcing this table was
# never safe before: it would have broken live tools rather than only the
# escalation it targets.
#
# The one that matters: `content` is the group whose server runs web_fetch, i.e.
# where prompt injection actually arrives. It must never carry a control-plane
# capability — `config` or `policies` — because a fetched page is attacker text.
# It used to be `code_change` that mattered most here; that scope is gone, along
# with every path by which the agent could edit source. See
# tests/test_security.py::SelfEditingIsRemovedTests for why it stays gone.
#
# `content` no longer carries `connectors`. It used to, and the comment here said
# why: mcp_server_content drove both the connector action bridge and the
# device-event ingest, so the group that reads attacker-controlled web pages held
# the token for every connected app's tools and every device actuation. The
# `connectors` group below was minted by install.sh and enforced by group_may()
# but had no server behind it, so SECURITY.md §3's claim that the
# injection-bearing group's blast radius is bounded was not true of connectors.
#
# The fix was to give the group its own server rather than to narrow the table:
# `agent/mcp_server_connectors/` now holds the generated per-app tools and the
# device-event tool, install.sh discovers it like any other, and `content` keeps
# only what its remaining tools actually call. A narrowed table with the tools
# still in mcp_server_content would have broken them instead.
INTERNAL_SCOPE_GROUPS: dict[str, frozenset[str]] = {
    "admin": frozenset({"logs", "perf", "config", "policies", "model"}),
    "content": frozenset({"documents", "model", "web"}),
    # Measured, not assumed: the only routes the moved tools call are
    # /internal/devices/events (device_events.mjs) and /internal/connector/<cid>/*
    # (the generated per-app tools). `model` was NOT added — nothing here asks for
    # it, and adding a scope on the assumption that a server probably needs it is
    # the same mistake as leaving `connectors` on `content`.
    "connectors": frozenset({"connectors"}),
    "productivity": frozenset({"learning", "model"}),
    "system": frozenset({"architecture", "model"}),
    "wellness": frozenset({"model"}),
}


def chmod_private(path: str) -> None:
    """Best-effort 0600 permissions for secret-bearing generated files."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def local_http_origin(url: str, *, allowed_ports: set[int] | None = None) -> str:
    """Validate and normalize an HTTP origin for host-local reverse proxies."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("origin must be an http(s) URL with a host")
    host = parsed.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
        is_local = ip.is_loopback
    except ValueError:
        is_local = host == "localhost"
    if not is_local:
        raise ValueError("origin must be loopback-only")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if allowed_ports and port not in allowed_ports:
        raise ValueError(f"origin port {port} is not allowed")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def allowed_proxy_path(path: str, prefixes: Iterable[str]) -> bool:
    """Path allowlist helper for reverse proxies.

    Prefixes are route-local and omit a leading slash, e.g. "api/" or "media/".
    Traversal-ish segments are rejected before prefix matching so future proxy
    routes inherit the same base hygiene.
    """
    clean = (path or "").lstrip("/")
    parts = [p for p in clean.split("/") if p]
    if any(p in {".", ".."} for p in parts):
        return False
    return any(clean == p.rstrip("/") or clean.startswith(p.rstrip("/") + "/")
               for p in prefixes)