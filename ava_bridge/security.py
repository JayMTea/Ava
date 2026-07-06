"""Shared security helpers for host callback routes and proxy surfaces.

Security-sensitive code should centralize policy decisions here instead of
open-coding allowlists in individual route handlers. New host capabilities get
one named internal scope and one narrow route check by default.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
from typing import Iterable
from urllib.parse import urlparse


INTERNAL_SCOPE_GROUPS: dict[str, frozenset[str]] = {
    "admin": frozenset({"logs", "perf", "config", "policies", "code_change"}),
    "content": frozenset({"documents", "run_gpu_job", "model", "web"}),
    "connectors": frozenset({"connectors"}),
    "productivity": frozenset({"learning", "model"}),
    "system": frozenset({"architecture", "model"}),
    "wellness": frozenset({"model"}),
}


def scoped_internal_token(base_token: str, group: str) -> str:
    """Derive the bearer token for one internal capability group."""
    return hmac.new(
        base_token.encode("utf-8"),
        f"ava-internal:{group}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def token_allows_scope(token: str, base_token: str, required: str | Iterable[str]) -> bool:
    """Return True only when a scoped token covers every required scope.

    The raw base token is intentionally not accepted by default. During a
    one-time migration, set AVA_INTERNAL_ALLOW_LEGACY_TOKEN=1 to let old sandbox
    installs continue while `agent/install.sh` is rerun.
    """
    if not token or not base_token:
        return False
    scopes = {required} if isinstance(required, str) else set(required)
    if not scopes:
        return False
    if os.environ.get("AVA_INTERNAL_ALLOW_LEGACY_TOKEN") == "1" \
            and hmac.compare_digest(token, base_token):
        return True
    for group, allowed in INTERNAL_SCOPE_GROUPS.items():
        if scopes.issubset(allowed):
            expected = scoped_internal_token(base_token, group)
            if hmac.compare_digest(token, expected):
                return True
    return False


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