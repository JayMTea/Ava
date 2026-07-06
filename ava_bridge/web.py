"""Host-side web access for Ava — self-hosted search + SSRF-guarded page reader.

Ava's sandbox never touches the internet directly. Her MCP tools call the
bridge's token-gated ``/internal/web/*`` routes and the HOST does the work here:

  * ``search(query)`` — queries a PRIVATE, loopback-only SearXNG instance
    (``config.WEB_SEARXNG_URL``). No third party, no API key. This endpoint is a
    fixed, trusted local address, so it is deliberately NOT run through the SSRF
    guard (it *is* loopback on purpose).

  * ``fetch(url)`` — downloads an arbitrary public web page and returns cleaned
    article text. This is the dangerous one: the host can reach internal services
    (a connected app on :8000, the router :8010, the DB, the Docker socket, cloud metadata,
    …), so ``fetch`` is wrapped in a strict SSRF guard that refuses any URL whose
    host resolves to a non-public IP, re-validating on every redirect hop.

Security posture (contained by design):
  - Public-IP-only: every resolved address must be globally routable; loopback,
    private, link-local, reserved, multicast and unspecified ranges are refused.
  - Scheme allowlist: only http/https.
  - Per-hop revalidation: redirects are followed manually, each hop re-checked,
    so a 302 cannot bounce the fetch into 127.0.0.1 / 169.254.169.254 / a LAN box.
  - Size + time caps: responses are streamed and aborted past WEB_FETCH_MAX_BYTES;
    a global timeout bounds every request.
  - Optional hostname denylist on top of the IP guard.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse, urljoin

import httpx

from . import config


class WebAccessError(Exception):
    """Raised for blocked/invalid web requests (surfaced to Ava as an error)."""


# Hostnames that must never be fetched even without resolving them (so we don't
# have to leak a DNS query to check). Covers loopback, link-local metadata and
# common internal TLDs.
_INTERNAL_HOST_EXACT = {
    "localhost", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "instance-data",
}
_INTERNAL_HOST_SUFFIX = (
    ".localhost", ".local", ".internal", ".lan", ".localdomain", ".home.arpa",
)


# ── SSRF guard ───────────────────────────────────────────────────────────────
def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # `is_global` already excludes private/loopback/link-local/reserved ranges,
    # but we spell the dangerous ones out too for clarity and defence in depth.
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        return False
    return addr.is_global


def _literal_ip(host: str) -> str | None:
    """Return the host if it is a literal IP (v4/v6, incl. bracketed), else None."""
    h = host.strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        ipaddress.ip_address(h)
        return h
    except ValueError:
        return None


def _resolve_public_ips(host: str) -> list[str]:
    """Resolve a hostname and require EVERY address to be public. Returns the IPs.

    Raises WebAccessError if resolution fails or any address is non-public — this
    blocks DNS entries that (partly) point at internal space. Only used when Tor
    is OFF (a local resolve would otherwise leak DNS to the ISP).
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise WebAccessError(f"cannot resolve host {host!r}: {e}") from e
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise WebAccessError(f"no addresses for host {host!r}")
    for ip in ips:
        if not _ip_is_public(ip):
            raise WebAccessError(
                f"refused: {host!r} resolves to non-public address {ip} "
                "(internal/loopback/link-local targets are blocked)")
    return ips


def _validate_fetch_url(url: str) -> tuple[str, str]:
    """Validate a URL for fetch. Returns (scheme, host). Raises WebAccessError.

    Tor ON  → name-based internal checks only (no local DNS resolve, so no DNS
              leak); Tor resolves remotely and its exits can't reach our LAN.
    Tor OFF → full local resolve; every A/AAAA record must be public.
    Literal private IPs are blocked in BOTH modes.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise WebAccessError(f"refused: only http/https URLs are allowed (got {scheme or 'none'!r})")
    host = parsed.hostname
    if not host:
        raise WebAccessError("refused: URL has no host")
    host_l = host.lower()

    for bad in config.WEB_DOMAIN_DENYLIST:
        if bad in host_l:
            raise WebAccessError(f"refused: host {host!r} is on the denylist")

    # A literal IP is checked directly in every mode (never trust a private IP).
    lit = _literal_ip(host)
    if lit is not None:
        if not _ip_is_public(lit):
            raise WebAccessError(
                f"refused: {lit} is a non-public address (internal targets blocked)")
        return scheme, host

    # Named host: block obvious internal names without resolving.
    if host_l in _INTERNAL_HOST_EXACT or host_l.endswith(_INTERNAL_HOST_SUFFIX):
        raise WebAccessError(f"refused: {host!r} is an internal hostname")

    if not config.WEB_TOR:
        _resolve_public_ips(host)      # Tor off: verify all resolved IPs public
    return scheme, host


# ── Search (private SearXNG) ─────────────────────────────────────────────────
def search(query: str, count: int | None = None) -> dict:
    """Query the private SearXNG instance and return normalized web results."""
    query = (query or "").strip()
    if not query:
        raise WebAccessError("empty query")
    limit = max(1, min(int(count or config.WEB_MAX_RESULTS), 20))
    url = f"{config.WEB_SEARXNG_URL}/search"
    params = {"q": query, "format": "json", "safesearch": "0", "language": "en"}
    try:
        with httpx.Client(timeout=config.WEB_TIMEOUT, follow_redirects=False) as client:
            r = client.get(url, params=params,
                           headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        raise WebAccessError(f"search backend unreachable: {e}") from e
    results = []
    for item in (data.get("results") or [])[:limit]:
        results.append({
            "title": (item.get("title") or "").strip(),
            "url": (item.get("url") or "").strip(),
            "snippet": (item.get("content") or "").strip(),
            "engine": item.get("engine") or "",
        })
    answers = [a for a in (data.get("answers") or []) if a]
    return {"query": query, "count": len(results), "results": results, "answers": answers}


# ── Fetch (SSRF-guarded reader) ──────────────────────────────────────────────
def _extract_text(html: str, url: str) -> str:
    """Extract main article text from HTML; fall back to a light strip."""
    try:
        import trafilatura
        txt = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=True,
            favor_precision=True, output_format="markdown")
        if txt and txt.strip():
            return txt.strip()
    except Exception:  # noqa: BLE001 — extraction is best-effort
        pass
    # Minimal fallback: drop tags crudely so Ava at least gets readable text.
    import re
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch(url: str) -> dict:
    """Fetch a public web page and return cleaned text. SSRF-guarded per hop.

    When config.WEB_TOR is set the request egresses through the Tor SOCKS5h proxy
    (pages see a Tor exit IP, DNS resolves inside Tor). FAIL-CLOSED: if Tor is
    unreachable we raise rather than silently falling back to your real IP.
    """
    url = (url or "").strip()
    if not url:
        raise WebAccessError("empty url")

    client_kwargs: dict = {
        "timeout": config.WEB_TIMEOUT,
        "follow_redirects": False,
        "headers": {"User-Agent": config.WEB_USER_AGENT},
    }
    if config.WEB_TOR:
        client_kwargs["proxy"] = config.WEB_TOR_SOCKS

    current = url
    hops = 0
    try:
        with httpx.Client(**client_kwargs) as client:
            while True:
                _validate_fetch_url(current)          # re-validate EVERY hop
                with client.stream("GET", current) as resp:
                    # Manual redirect handling so each Location is SSRF-checked.
                    if resp.is_redirect:
                        hops += 1
                        if hops > config.WEB_FETCH_MAX_REDIRECTS:
                            raise WebAccessError("too many redirects")
                        loc = resp.headers.get("location", "")
                        if not loc:
                            raise WebAccessError("redirect without location")
                        current = urljoin(current, loc)
                        continue

                    resp.raise_for_status()
                    ctype = resp.headers.get("content-type", "")
                    if ctype and not any(t in ctype.lower()
                                         for t in ("text/html", "text/plain",
                                                   "application/xhtml", "application/xml",
                                                   "text/xml")):
                        raise WebAccessError(f"unsupported content-type: {ctype}")

                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > config.WEB_FETCH_MAX_BYTES:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    final_url = str(resp.url)
                    break
    except (httpx.ProxyError, httpx.ConnectError) as e:
        if config.WEB_TOR:
            # Do NOT fall back to clearnet — that would leak the real IP.
            raise WebAccessError(
                "Tor proxy unreachable — refusing to fetch over clearnet "
                f"(fail-closed to protect your IP). Is ava-tor running? [{e}]") from e
        raise WebAccessError(f"connection failed: {e}") from e
    except httpx.HTTPError as e:
        raise WebAccessError(f"fetch failed: {e}") from e

    html = raw.decode("utf-8", errors="replace")
    text = _extract_text(html, final_url)
    truncated = len(text) > config.WEB_FETCH_MAX_CHARS
    if truncated:
        text = text[:config.WEB_FETCH_MAX_CHARS].rstrip() + "\n\n[...truncated]"
    return {
        "url": final_url,
        "bytes": total,
        "truncated": truncated,
        "via_tor": bool(config.WEB_TOR),
        "text": text,
    }
