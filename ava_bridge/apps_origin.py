"""Serve embedded connector apps from their OWN browser origin.

**The hole this closes.** Ava reverse-proxies a connected app's UI same-origin under
`/apps/<cid>/`, and `AppFrame` sandboxes the iframe `allow-scripts allow-forms
allow-same-origin`. `allow-scripts` + `allow-same-origin` together are a documented
no-op pairing: the frame keeps Ava's origin. Ava serves no CSP, and `/api/hub/*` has
no CSRF or Origin check — the session cookie is the only gate, and a same-origin
frame sends it. So an embedded app's JavaScript could do:

    fetch('/api/hub/approvals').then(r => r.json())
      .then(j => j.pending.forEach(p => fetch('/api/hub/approvals/' + p.id,
                                              {method: 'POST'})))

and approve Ava's own consent prompts. It could also read `parent.*`, since it is
same-origin with the shell. `docs/CONNECTOR_SDK.md` accepts this for a locally-run
app the owner installed ("treat the embedded app as trusted code"). It is a
different claim for a remotely-served app whose bundle can change without the owner
touching Ava.

**Why an Origin check cannot fix it.** Because the proxy makes the app *genuinely*
same-origin, `Origin`, `Sec-Fetch-Site` and any CSRF token are byte-identical to the
real SPA's. There is no header that distinguishes them. The only fixes are to stop
the frame being same-origin, or to stop serving it from Ava's origin at all.

**The mechanism: a second hostname on the same port.** Not a second listener —
that would mean new deployment surface (systemd unit, Docker port publishing, two
uvicorn binds) for a boundary the browser can enforce from a hostname alone. Two
hostnames resolving to one address are two origins to a browser: separate cookie
jars, cross-origin `fetch` without credentials, no `parent` access. Same bind, same
port, same reverse proxy.

    apps.origin: "http://apps.ava.local:8096"     # ava.yaml, or AVA_APPS_ORIGIN

Set it and:

  * `/apps/*` is served ONLY when the request's host matches that origin;
  * everything else — `/api/*`, the SPA, `/media/*` — is refused ON that host, so
    the embedded app's JS has nothing to reach even with a stolen cookie;
  * `AppFrame` points the iframe at the absolute apps origin with a short-lived,
    cid-bound token, because Ava's session cookie does not cross origins.

**Default off.** Unset, behaviour is exactly as before and the hole remains — which
is why `warning()` exists and the Setup page surfaces it. Turning it on requires the
owner to make a second name resolve to the box (a `hosts` entry, a DNS record, or a
Tailscale alias); that is a real step, and pretending otherwise by defaulting it on
would break every existing install's app tiles.

**The token.** HMAC over `(cid, expiry)` keyed on the same root secret as the
internal token, so it needs no new store. Short TTL, single connector, and it grants
exactly one thing: permission to load that app's proxy on the apps origin. It is not
a session — the apps origin has no session, deliberately.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlsplit

from . import config, settings

# Long enough to survive a slow first paint and a user switching tabs before the
# frame loads; short enough that a token copied out of a URL is stale by the time
# anyone reuses it. The frame re-fetches on every mount, so nothing depends on it
# living longer.
TOKEN_TTL_S = 300


def configured() -> str | None:
    """The apps origin, normalised to `scheme://host[:port]`, or None when unset."""
    raw = settings.get("apps.origin", "", env="AVA_APPS_ORIGIN")
    raw = str(raw or "").strip().rstrip("/")
    if not raw:
        return None
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _origin_netloc() -> str | None:
    o = configured()
    return urlsplit(o).netloc.lower() if o else None


def request_host(request) -> str:
    """The host the BROWSER used, honouring a trusted proxy's forwarded header.

    `X-Forwarded-Host` is read only from a trusted proxy — the same restriction
    `serve.py` applies to `proxy_headers` for the scheme, and for the same reason:
    accepting it from an arbitrary peer would let a caller claim to be on whichever
    origin gets it past this check.
    """
    client = getattr(getattr(request, "client", None), "host", None) or ""
    if client in config.TRUSTED_PROXIES:
        fwd = request.headers.get("x-forwarded-host", "")
        if fwd:
            return fwd.split(",")[0].strip().lower()
    return (request.headers.get("host", "") or "").split(",")[0].strip().lower()


def on_apps_host(request) -> bool:
    """Is this request addressed to the apps origin? False when unconfigured."""
    want = _origin_netloc()
    if not want:
        return False
    got = request_host(request)
    if got == want:
        return True
    # A default-port origin may arrive without the port, and vice versa.
    def _bare(n: str) -> str:
        return n.rsplit(":", 1)[0] if n.endswith((":80", ":443")) else n
    return _bare(got) == _bare(want)


def is_app_path(path: str) -> bool:
    return path == "/apps" or path.startswith("/apps/")


def refuses(request, path: str) -> str | None:
    """Why this request must be refused, or None to let it through.

    Two symmetric rules, and BOTH are needed. Serving `/apps/*` only on the apps
    host moves the app off Ava's origin; refusing everything else on that host is
    what makes the move worth anything, because otherwise the app's JS simply calls
    `/api/hub/*` on its own origin and the cookie it lacks is the only thing
    standing in the way. Belt and braces: no cookie AND no route.
    """
    if not configured():
        return None
    apps_host = on_apps_host(request)
    if apps_host and not is_app_path(path):
        return "this host serves connector apps only"
    if is_app_path(path) and not apps_host:
        return "connector apps are served from the apps origin"
    return None


def _sign(cid: str, exp: int) -> str:
    return hmac.new(config.INTERNAL_TOKEN.encode(),
                    f"ava-app-embed:{cid}:{exp}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def mint(cid: str, ttl_s: int = TOKEN_TTL_S) -> str:
    exp = int(time.time()) + int(ttl_s)
    return f"{exp}.{_sign(cid, exp)}"


def verify(cid: str, token: str) -> bool:
    """Constant-time check that `token` was minted for `cid` and has not expired."""
    try:
        raw_exp, sig = str(token or "").split(".", 1)
        exp = int(raw_exp)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sign(cid, exp))


def embed_url(cid: str, query: str = "") -> str | None:
    """The absolute URL the iframe should load, or None when unconfigured."""
    o = configured()
    if not o:
        return None
    sep = "&" if query else ""
    return f"{o}/apps/{cid}/?{query}{sep}t={mint(cid)}"


def cid_from_path(path: str) -> str:
    """`/apps/<cid>/anything` -> `<cid>`, else ''."""
    parts = path.split("/", 3)
    return parts[2] if len(parts) > 2 else ""


def cookie_name(cid: str) -> str:
    # cid is validated `[a-z][a-z0-9_-]{1,31}` by the Hub before a manifest exists,
    # so it is already a legal cookie-name suffix.
    return f"ava_app_{cid}"


def authorize(request, path: str) -> tuple[bool, str | None, str]:
    """Authorize an `/apps/*` request ON the apps origin.

    Returns `(allowed, token_to_set_as_cookie, reason)`.

    The apps origin has no Ava session — that is the entire point — so the first
    request carries `?t=<token>` from the URL `AppFrame` was handed. Assets the app
    then requests relative to its own document carry no query string, so the token
    is exchanged once for a cookie scoped to THIS origin. That cookie is not an Ava
    session and grants nothing but this one app's proxy: it is the same minted
    token, verified against the cid in the path on every request.
    """
    cid = cid_from_path(path)
    if not cid:
        return False, None, "no connector id in path"
    from_query = request.query_params.get("t") or ""
    if from_query and verify(cid, from_query):
        return True, from_query, ""
    from_cookie = request.cookies.get(cookie_name(cid)) or ""
    if from_cookie and verify(cid, from_cookie):
        return True, None, ""
    if from_query or from_cookie:
        return False, None, "embed token expired — reload the app in Ava"
    return False, None, "no embed token"


def apply_cookie(response, cid: str, token: str, secure: bool) -> None:
    """Pin the exchanged token to this origin and this app's path.

    `httponly` so the app's own JS cannot read it, `samesite=lax` so it is not sent
    on cross-site requests, and `path=/apps/<cid>/` so one embedded app's cookie is
    not offered to another's proxy.
    """
    response.set_cookie(
        cookie_name(cid), token, max_age=TOKEN_TTL_S, httponly=True,
        samesite="lax", secure=secure, path=f"/apps/{cid}/")


def warning() -> dict:
    """What the Setup page shows. Says the untrue-by-default thing out loud."""
    if configured():
        return {"ok": True, "origin": configured()}
    return {
        "ok": False,
        "origin": None,
        "detail": (
            "Embedded apps are served from Ava's own origin, so an app's "
            "JavaScript runs with your session and can call Ava's API — including "
            "approving Ava's consent prompts. Set apps.origin to a second hostname "
            "pointing at this machine to isolate them."),
    }
