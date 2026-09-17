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

**Two lifetimes, and what happens when the second runs out.** The token in the
iframe's URL lives `TOKEN_TTL_S`; the cookie it is exchanged for lives `COOKIE_TTL_S`
and slides forward on use. A frame that outlives even that is not left staring at
JSON: a framed navigation on a dead token is answered with `reconnect_page()`, which
asks the shell for a fresh URL, and the shell (`AppFrame`) re-mints on its own when
the page comes back from the background. The constants below say why the numbers
differ.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlsplit

from . import config, settings

# The token lives in two places with different exposure, so it has two lifetimes.
#
# THE URL TOKEN is what `AppFrame` is handed and puts in the iframe's `src`. A URL
# leaks — browser history, a screenshot, a Referer to the app's own origin — so it
# is long enough to survive a slow first paint and a user switching tabs before the
# frame loads, and short enough that a copied one is stale by the time anyone
# reuses it. The frame re-fetches on every mount, so a fresh mount never depends on
# an old one.
TOKEN_TTL_S = 300
#
# THE COOKIE the URL token is exchanged for is HttpOnly, Secure, SameSite=Lax and
# scoped to `/apps/<cid>/` on the apps origin: never in a URL, unreadable by the
# app's own JS. It used to inherit the five minutes, renewed only by traffic
# (`_renewal`), and that was measured as a dead panel on every phone: iOS freezes a
# backgrounded page, so no request arrives to renew anything, and the first tap
# after unlocking got `embed token expired` as the app's whole UI — with no reload
# button on a home-screen app to get out of it. Twelve hours is a working day of
# being left alone. Renewal still slides it forward on use, and the shell re-mints
# on resume past it, so this is a floor on idle survival, not a ceiling on a
# session. The security argument is unchanged: the cookie grants one app's proxy on
# one origin, and the app behind it still runs its own sign-in.
COOKIE_TTL_S = 12 * 60 * 60


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


def is_app_api_path(path: str) -> bool:
    """`/apps/<cid>/api[/...]` — the browser data-proxy under an app's prefix."""
    parts = path.split("/")
    return len(parts) >= 4 and parts[1] == "apps" and bool(parts[2]) \
        and parts[3] == "api"


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
        # One carve-out: the ui.api data-proxy. A NATIVE app view is part of
        # Ava's own bundle, on Ava's own origin, holding Ava's session — it
        # has no iframe, no embed token, and no way to be handed one. Its data
        # calls go to /apps/<cid>/api/* and must keep working on the main
        # host, where the session cookie (checked downstream in auth_gate) is
        # the gate; refusing them here broke every native view the moment the
        # split was turned on. This does not weaken the iframe boundary: an
        # embedded app's JS lives on the apps origin, where a cross-origin
        # call to the main host carries no session cookie — and on its own
        # host the embed token stays required. The proxy refuses to serve app
        # UI documents through this carve-out (phone_bridge.app_api_proxy
        # blocks the no-ui.api fall-through across the split), so it admits
        # the app's data, never its documents.
        if is_app_api_path(path):
            return None
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


def _renewal(cid: str, token: str) -> str | None:
    """A replacement cookie token when `token` is past half-life, else None.

    Sliding renewal is what keeps an OPEN panel alive past COOKIE_TTL_S. The
    TTL used to be absolute: once the frame's token aged out, every request —
    an SSE reconnect, a click, an asset — got a raw 403 while the panel looked
    perfectly healthy. Now every authorized request past half-life hands back a
    fresh token for the middleware to set as the cookie, so the clock re-arms
    for as long as the panel is in use, and only one idle for the FULL TTL ever
    sees its token die. A top-level return to such a dead panel re-enters
    through the shell (auth._shell_bounce -> /#<cid> -> a freshly minted embed
    URL); a framed one gets `reconnect_page`, which asks the shell to do the
    same. Re-minting FOR an expired token is deliberately not done: an expired
    token is exactly as unauthenticated as no token, and honouring it would
    make the TTL decorative.

    Half-life rather than every request: a fresh token's burst of asset loads
    should not carry a Set-Cookie per response, and renewing early buys
    nothing — the replacement is identical in power (same cid, same TTL).
    The security property is unchanged: tokens stay cid-bound and bounded,
    and a token for app A still verifies for app A alone.
    """
    try:
        exp = int(str(token).split(".", 1)[0])
    except ValueError:
        return None
    if exp - int(time.time()) <= COOKIE_TTL_S // 2:
        return mint(cid, COOKIE_TTL_S)
    return None


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
    session and grants nothing but this one app's proxy: a token of the same shape,
    minted for the cookie's own life (COOKIE_TTL_S — the URL token's job ends at
    the exchange), verified against the cid in the path on every request, and
    re-minted on a sliding half-life (`_renewal`) so an open panel outlives even
    that for exactly as long as it stays in use.
    """
    cid = cid_from_path(path)
    if not cid:
        return False, None, "no connector id in path"
    from_query = request.query_params.get("t") or ""
    if from_query and verify(cid, from_query):
        return True, mint(cid, COOKIE_TTL_S), ""
    from_cookie = request.cookies.get(cookie_name(cid)) or ""
    if from_cookie and verify(cid, from_cookie):
        return True, _renewal(cid, from_cookie), ""
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
        cookie_name(cid), token, max_age=COOKIE_TTL_S, httponly=True,
        samesite="lax", secure=secure, path=f"/apps/{cid}/")


def apply_grant_cookie(response, request, cid: str) -> None:
    """Establish the app session on the authenticated grant response when possible.

    Cookies are scoped to host and path, not port. When the apps origin uses
    another port on Ava's hostname, the shell can set the app's cookie before
    navigating its frame. This also repairs older cached app shells whose worker
    swallows that navigation and therefore never exchanges the URL token.
    Different hostnames still exchange the token on the apps origin as usual.
    Call only after authorizing the owner and checking that the app exists.
    """
    origin = configured()
    if not origin:
        return
    app = urlsplit(origin)
    host = urlsplit("//" + request_host(request)).hostname
    if not host or host.lower() != (app.hostname or "").lower():
        return
    apply_cookie(response, cid, mint(cid, COOKIE_TTL_S), secure=app.scheme == "https")


def wants_frame_document(request) -> bool:
    """A GET that will RENDER the answer inside a frame, where JSON is a wall.

    `iframe`/`frame`/`embed`/`object` are the framed navigations. `document` is
    a top-level one and belongs to `auth._shell_bounce`; `empty` is app JS, which
    must keep getting JSON it can parse. Without Fetch Metadata at all (curl, an
    old engine) the Accept header decides — the same fallback
    `phone_bridge._wants_document` uses for the unreachable page.
    """
    if getattr(request, "method", "GET") != "GET":
        return False
    dest = (request.headers.get("sec-fetch-dest") or "").lower()
    if dest:
        return dest in ("iframe", "frame", "embed", "object")
    return "text/html" in (request.headers.get("accept") or "")


def shell_origin() -> str:
    """`server.public_url` as a bare origin — the only place a frame may report to."""
    parts = urlsplit(config.PUBLIC_URL)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return config.PUBLIC_URL.rstrip("/")


def resume_path(request, cid: str) -> str:
    """Where the frame was going, relative to the app, minus the spent token.

    `/apps/<cid>/reports/7?x=1&t=…` -> `/reports/7?x=1`. The shell feeds it back
    through `appFrameDestination`, which re-validates the prefix, so the worst a
    hostile value can do here is land the reopened app on its own 404.
    """
    from urllib.parse import quote, unquote, urlencode
    raw = str(getattr(request.url, "path", "") or "")
    prefix = f"/apps/{cid}"
    rel = raw[len(prefix):] if raw.startswith(prefix) else "/"
    rel = quote(unquote(rel), safe="/")[:2000] or "/"
    if not rel.startswith("/"):
        rel = "/" + rel
    items = getattr(request.query_params, "multi_items", None)
    pairs = list(items()) if callable(items) else list(request.query_params.items())
    query = urlencode([(k, v) for k, v in pairs if k != "t"], doseq=True)
    return f"{rel}?{query}" if query else rel


def reconnect_page(cid: str, label: str, reason: str, theme: str, path: str) -> str:
    """The document a DEAD FRAME renders instead of `{"error":"forbidden"}`.

    Served by the apps origin itself, so nothing about framing changes: the
    frame is already allowed to show this origin, and the page carries no CSP a
    parent could trip over. Its one job is to tell the shell which app died and
    where it was, so `AppFrame` can fetch a fresh embed URL and reload the frame
    there — what the owner would do by hand with a reload button, which a
    home-screen app on a phone does not have.

    `postMessage` goes to `shell_origin()` and nowhere else: a parent on any
    other origin receives nothing, and there is nothing in the message worth
    having anyway (a cid and a path the parent already put in the URL). Opened
    with no parent — a browser that sent no Fetch Metadata at the top level —
    it sends itself to the shell instead, the way `auth._shell_bounce` does for
    the ones that say `document`.

    Still a 403 from the caller's side: the refusal is unchanged, only its body
    knows how to get the owner a fresh token.
    """
    import html as _html
    import json as _json

    def js(value) -> str:
        # `</` inside a string literal would end the <script>; the escape is
        # invisible to JSON and to JS.
        return _json.dumps(value).replace("</", "<\\/")

    dark = theme != "light"
    bg, fg, dim = ("#16181d", "#e8eaed", "#9aa0a6") if dark else ("#fbfbfc", "#1f2124", "#5f6368")
    card, edge = ("#1e2127", "#2c3038") if dark else ("#ffffff", "#e3e5e9")
    shell = shell_origin()
    script = (
        "<script>(function(){"
        "var shell=" + js(shell) + ",tile=" + js(f"{shell}/#{cid}") + ","
        "msg=" + js({"type": "ava:embed-expired", "cid": cid, "path": path}) + ";"
        "function ask(){"
        "if(window.parent===window){window.location.replace(tile);return;}"
        "try{window.parent.postMessage(msg,shell);}catch(e){}"
        "}"
        "var b=document.getElementById('reconnect');if(b){b.addEventListener('click',ask);}"
        "ask();"
        "})();</script>"
    )
    return f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reconnecting {_html.escape(label)}</title>
<style>
 html,body{{margin:0;height:100%;background:{bg};color:{fg};
   font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
 .wrap{{height:100%;display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box}}
 .card{{max-width:30rem;background:{card};border:1px solid {edge};border-radius:12px;padding:22px 24px}}
 h1{{margin:0 0 8px;font-size:15px;font-weight:600}}
 p{{margin:0 0 12px;color:{dim}}}
 button{{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid {edge};background:{bg};color:{fg};cursor:pointer}}
 details{{margin-top:16px}}
 summary{{color:{dim};font-size:12px;cursor:pointer}}
 pre{{margin:8px 0 0;padding:10px;background:{bg};border:1px solid {edge};border-radius:8px;
   font-size:11px;color:{dim};white-space:pre-wrap;word-break:break-all;overflow-x:auto}}
</style>
<div class="wrap"><div class="card">
 <h1>Reconnecting {_html.escape(label)}…</h1>
 <p>Ava's link to this app timed out while it was idle. It reconnects on its own; if this stays, use the button.</p>
 <p><button id="reconnect" type="button">Reconnect</button></p>
 <details><summary>Technical detail</summary><pre>{_html.escape(reason)}</pre></details>
</div></div>
{script}"""


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
