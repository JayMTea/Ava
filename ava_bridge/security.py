"""Shared security helpers for host callback routes and proxy surfaces.

Security-sensitive code should centralize policy decisions here instead of
open-coding allowlists in individual route handlers. New host capabilities get
one named internal scope and one narrow route check by default.
"""
from __future__ import annotations

import ipaddress
import os
from typing import Iterable
from urllib.parse import urlparse


# What each MCP capability group may reach on /internal/*. Enforced by
# ava_bridge/internal.group_may(), which ava_bridge/auth.auth_gate calls for every
# /internal request.
#
# These sets are derived from what each agent/mcp_server_<group>/ ACTUALLY calls,
# not from what sounds tidy — `content` carries connectors because
# mcp_server_content drives the connector action bridge and the device-event
# ingest. Getting that wrong is why enforcing this table was never safe before:
# it would have broken live tools rather than only the escalation it targets.
#
# The one that matters: `content` is the group whose server runs web_fetch, i.e.
# where prompt injection actually arrives. It must never carry `code_change`.
INTERNAL_SCOPE_GROUPS: dict[str, frozenset[str]] = {
    "admin": frozenset({"logs", "perf", "config", "policies", "code_change",
                        "model"}),
    "content": frozenset({"documents", "run_gpu_job", "model", "web",
                          "connectors"}),
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