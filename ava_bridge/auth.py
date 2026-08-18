"""Authentication: password gate, signed-cookie sessions, login throttle, middleware.

Step-zero gate for the Experience plane (:8445). The Tailnet already limits *who*
can reach this port; this adds a *something-you-know* factor so a logged-in device
on the tailnet still can't drive Ava without the password. A single shared password
issues a signed (HMAC) session cookie. Stateless: survives restarts, no server-side
session store to leak.
"""
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import config, state


from .security import constant_time_equals
from .security import secure_opener as _secure_opener


def _auth_secret() -> bytes:
    """HMAC key for signing session cookies. Persisted so logins survive restarts."""
    env = os.environ.get("AVA_SECRET")
    if env:
        return env.encode()
    path = os.path.join(config.CHATS_DIR, ".secret")
    try:
        with open(path, "rb") as f:
            data = f.read()
            if data:
                return data
    except FileNotFoundError:
        pass
    data = secrets.token_bytes(32)
    with open(path, "wb", opener=_secure_opener) as f:
        f.write(data)
    os.chmod(path, 0o600)
    return data


_PASSWORD_FILE = os.path.join(config.CHATS_DIR, "auth_password")


def current_password() -> str:
    """The active login password: env AVA_PASSWORD, else data/auth_password, else
    "" (unset). Unlike before, this NEVER auto-generates — an empty result means
    first-run, so the bridge shows a "set your admin password" screen instead of a
    dead login wall. Read fresh each call so a just-set password takes effect."""
    env = os.environ.get("AVA_PASSWORD")
    if env:
        return env
    try:
        with open(_PASSWORD_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def needs_setup() -> bool:
    """True on a fresh install with no password configured (first-run wizard)."""
    return not current_password()


def set_password(pw: str) -> None:
    """Persist the admin password (0600). Used by the first-run /setup screen."""
    with open(_PASSWORD_FILE, "w", encoding="utf-8", opener=_secure_opener) as f:
        f.write(pw.strip() + "\n")
    os.chmod(_PASSWORD_FILE, 0o600)


_AUTH_SECRET: bytes | None = None


def _secret() -> bytes:
    """The signing key, cached. Read through a function rather than bound once at
    import so `rotate_secret()` can invalidate every outstanding cookie without a
    process restart."""
    global _AUTH_SECRET
    if _AUTH_SECRET is None:
        _AUTH_SECRET = _auth_secret()
    return _AUTH_SECRET


def rotate_secret() -> bool:
    """Mint a new signing key: every cookie signed with the old one stops
    validating. This is the revoke primitive — there is no server-side session
    store to evict from, so invalidating the key IS "log everyone out".

    Returns False when AVA_SECRET pins the key in the environment, since the file
    would be ignored and the caller must not report a revoke that did not happen.
    """
    global _AUTH_SECRET
    if os.environ.get("AVA_SECRET"):
        return False
    data = secrets.token_bytes(32)
    path = os.path.join(config.CHATS_DIR, ".secret")
    with open(path, "wb", opener=_secure_opener) as f:
        f.write(data)
    os.chmod(path, 0o600)
    _AUTH_SECRET = data
    return True


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def _make_token() -> str:
    """`exp.sid.sig`.

    The `sid` is what makes two sessions distinguishable. Without it the token was
    just a signed expiry stamp, so every login within the same second produced a
    byte-identical cookie — two devices shared one indistinguishable session, and
    "log out my other device" was not expressible. It also gives the cookie
    entropy that does not depend on the clock.
    """
    exp = str(int(time.time()) + config.SESSION_TTL)
    sid = secrets.token_urlsafe(12)
    payload = f"{exp}.{sid}"
    return f"{payload}.{_sign(payload)}"


def _valid_token(tok: str) -> bool:
    # Exactly three parts. Legacy two-part `exp.sig` cookies are rejected rather
    # than accepted for compatibility: they predate session ids, and honouring
    # them would keep the weaker format alive indefinitely. The cost is one
    # re-login per user at upgrade.
    parts = (tok or "").split(".")
    if len(parts) != 3:
        return False
    exp, sid, sig = parts
    if not exp or not sid:
        return False
    # constant_time_equals, not hmac.compare_digest: the latter raises
    # TypeError on a `str` holding any non-ASCII character, and `sig` is
    # attacker-supplied cookie text. That is an unauthenticated 500 on every
    # route that reads a session — security.py exists for exactly this and
    # says so; these two call sites were simply missed.
    if not constant_time_equals(sig, _sign(f"{exp}.{sid}")):
        return False
    try:
        return int(exp) > int(time.time())
    except ValueError:
        return False


def is_authed(request: Request) -> bool:
    return _valid_token(request.cookies.get(config.COOKIE_NAME, ""))


# --- who is asking, and over what ---------------------------------------------

def _is_loopback(host: str) -> bool:
    """True only for a host we can PARSE and that is loopback.

    Fails closed on anything unparseable — including Starlette's TestClient, whose
    `request.client.host` is the literal string "testclient". A gate that treated
    "cannot parse" as "local" would be open to anything that confused it, so the
    QA suite passes an explicit client address instead (qa/conftest.py).
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """The caller's address, honouring X-Forwarded-For ONLY from a trusted peer.

    One resolver for the whole app. It also fixes a live defect: the login
    throttle keyed on `request.client.host`, so behind any reverse proxy every
    device on the network shared a single lockout bucket — eight wrong guesses
    from one phone locked out the house.
    """
    peer = (request.client.host if request.client else "") or ""
    if peer in config.TRUSTED_PROXIES:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            # Last hop is the one our trusted proxy actually spoke to; earlier
            # entries are client-supplied and forgeable.
            return fwd.split(",")[-1].strip() or peer
    return peer


def request_is_secure(request: Request) -> bool:
    """Did this request reach us over TLS?"""
    if request.url.scheme == "https":
        return True
    peer = (request.client.host if request.client else "") or ""
    if peer in config.TRUSTED_PROXIES:
        proto = request.headers.get("x-forwarded-proto", "")
        if proto:
            return proto.split(",")[0].strip().lower() == "https"
    return False


def cookie_secure_for(request: Request) -> bool:
    """Whether to mark the session cookie Secure for THIS request."""
    if config.COOKIE_SECURE is not None:
        return bool(config.COOKIE_SECURE)
    return request_is_secure(request)


def set_session_cookie(resp: Response, request: Request | None = None) -> None:
    secure = cookie_secure_for(request) if request is not None else bool(config.COOKIE_SECURE)
    resp.set_cookie(config.COOKIE_NAME, _make_token(), max_age=config.SESSION_TTL,
                    httponly=True, secure=secure, samesite="lax", path="/")


# --- first-run claim ----------------------------------------------------------
# `/setup` is public (it has to be — there is no password yet) and POST /setup
# only checked `needs_setup()`. On a non-loopback bind that means the first device
# on the network to find the box sets the admin password, and the owner is locked
# out of their own install. Refusing outright is not the answer either: a headless
# server with no local browser must still be claimable.
#
# So: loopback claims freely; anyone else presents a token printed to the server's
# own console. Same shape as Jupyter's token and Pi-hole v6's first-run password.
# NOT Home Assistant, which this comment used to cite: HA's onboarding has no
# token and no secret to present — it trusts whoever reaches the port first, which
# is the design this gate exists to refuse. Citing it as the same shape argued
# the opposite of what the code does.

_CLAIM_FILE = os.path.join(config.CHATS_DIR, "setup_claim")


def claim_token() -> str:
    """The current first-run claim token, minting one if absent. Empty once the
    instance has been claimed (the file is removed when a password is set)."""
    if not needs_setup():
        return ""
    try:
        with open(_CLAIM_FILE, encoding="utf-8") as f:
            existing = f.read().strip()
            if existing:
                return existing
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(16)
    try:
        with open(_CLAIM_FILE, "w", encoding="utf-8", opener=_secure_opener) as f:
            f.write(tok + "\n")
        os.chmod(_CLAIM_FILE, 0o600)
    except OSError:
        return ""          # unwritable AVA_HOME: never block setup on bookkeeping
    return tok


def clear_claim() -> None:
    """Called once a password exists — the window is over."""
    try:
        os.remove(_CLAIM_FILE)
    except OSError:
        pass


def may_claim(request: Request) -> bool:
    """May this caller run first-run setup?"""
    # A loopback PEER is not enough: DNS rebinding produces exactly that shape
    # from a page the owner merely visited. The gate already refuses an untrusted
    # Host, and this is the belt to that braces — /setup sets the admin password,
    # so it should not depend on one check.
    if _is_loopback(client_ip(request)) and host_is_trusted(request)[0]:
        return True
    want = claim_token()
    if not want:
        return False
    got = (request.query_params.get("claim")
           or request.headers.get("x-ava-setup-claim") or "")
    # Same reason, and it lands on the worst possible screen: a first-run
    # owner pasting a token that picked up a smart quote or an accent got a
    # blank Internal Server Error instead of the "that token was not
    # accepted" page written for them.
    return bool(got) and constant_time_equals(got.strip(), want)


def same_site_write(request: Request) -> tuple[bool, str]:
    """(ok, reason). Did the browser say this state-changing POST is its own?

    `POST /setup` takes exactly `password` and `confirm`, which makes a
    cross-site auto-submitting form a CORS *simple* request: no preflight, and
    `Host: 127.0.0.1:8096` sails through `host_is_trusted()`. On the Docker path
    `may_claim()` happens to stop it, because the peer is the bridge gateway
    rather than loopback — but on BARE METAL a loopback caller passes the gate
    with no token at all, so any page the owner visits during the first-run
    window could set the admin password. The gate was never a CSRF defence; it
    only looked like one on the install where the peer address happened to fail.

    `Sec-Fetch-Site` is decided by the browser and cannot be set by page script,
    so it is authoritative when present. `Origin` is the fallback for clients
    that predate it. Neither present means no browser sent this, and a request
    no browser sent cannot be cross-site request forgery — so it is allowed, and
    curl, the health probes and the QA suite keep working.
    """
    sfs = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if sfs:
        # "none" is a direct navigation (typed, bookmarked); "same-origin" is our
        # own page. "same-site" and "cross-site" both mean another origin drove it.
        if sfs in ("same-origin", "none"):
            return True, ""
        return False, f"Sec-Fetch-Site: {sfs}"
    origin = (request.headers.get("origin") or "").strip()
    if origin and origin.lower() not in ("null", "undefined"):
        host = (request.headers.get("host") or "").strip().lower()
        try:
            netloc = urlsplit(origin).netloc.lower()
        except ValueError:
            return False, f"unparseable Origin: {origin!r}"
        if netloc and host and netloc == host:
            return True, ""
        return False, f"Origin {origin!r} is not this host"
    return True, ""


def in_container() -> bool:
    """Best-effort: are we inside the shipped Docker image?"""
    return os.path.exists("/.dockerenv")


def claim_hint() -> str:
    """What to tell a remote caller who has no token. Names the file rather than
    the value: the point is that you must be able to read the server's disk.

    Under Docker that file lives on the CONTAINER's filesystem, so printing the
    bare path sends the reader to a path that does not exist on their host —
    and every browser hits this gate on the compose install, because the peer
    address the container sees is the bridge gateway, not 127.0.0.1. Give the
    containerized reader a command that works from where they actually are.
    """
    return (f"Run this on the machine Ava is installed on:\n"
            f"    {claim_read_cmd()}\n"
            f"    ({claim_windows_note()})\n"
            f"then open  <this-url>/setup?claim=<token>")


def claim_read_cmd() -> str:
    """The command that prints the token, for wherever the reader actually is."""
    if in_container():
        return f"cd deploy && docker compose exec ava cat {_CLAIM_FILE}"
    return f"cat {_CLAIM_FILE}"


def claim_windows_note() -> str:
    """The Git Bash caveat, kept SEPARATE from the command rather than baked in.

    Git Bash is the documented Windows shell, and MSYS rewrites the container
    path in that command into a Windows one before docker ever sees it — so the
    read returns nothing and the reader is left holding a command that appears to
    work and prints an empty token. That is the same defect deploy/install.sh
    carries MSYS_NO_PATHCONV=1 to avoid, and it lands here on the one screen a
    stuck owner reaches.

    Not inlined into claim_read_cmd() because Command Prompt cannot set an env
    var inline and runs the unprefixed command fine: baking the prefix in would
    fix one Windows shell by breaking the other.
    """
    return "on Windows in Git Bash, prefix that with MSYS_NO_PATHCONV=1"


def login_locked(ip: str) -> bool:
    with state.login_lock:
        rec = state.login_fails.get(ip)
        if not rec:
            return False
        count, first = rec
        if time.time() - first > config.LOGIN_WINDOW:
            state.login_fails.pop(ip, None)
            return False
        return count >= config.LOGIN_MAX


def login_record(ip: str, ok: bool) -> None:
    """Record a login attempt for the per-IP throttle.

    A SUCCESS clears that IP's failure count outright — the throttle exists to slow
    guessing, not to punish someone who mistyped once and then got it right. A
    failure increments within config.LOGIN_WINDOW and starts a fresh window
    outside it, so the counter is a sliding window rather than a lifetime total.

    `ip` comes from client_ip(), which honours X-Forwarded-For only from a trusted
    proxy — so this cannot be poisoned into throttling somebody else.
    """
    with state.login_lock:
        if ok:
            state.login_fails.pop(ip, None)
            return
        now = time.time()
        rec = state.login_fails.get(ip)
        if not rec or now - rec[1] > config.LOGIN_WINDOW:
            state.login_fails[ip] = [1, now]
        else:
            rec[0] += 1


_PUBLIC_PATHS = {"/login", "/logout", "/setup", "/api/health", "/favicon.ico",
                 # The vector tab icon, public for the same reason the .ico is:
                 # the sign-in page is rendered before any cookie exists.
                 "/favicon.svg",
                 # PWA shell: browsers fetch the manifest and service worker
                 # without credentials context — they must not bounce to /login.
                 "/manifest.webmanifest", "/sw.js",
                 # Ava's own app icons, which the PUBLIC manifest and index.html
                 # point at. An installing browser fetches these with no session,
                 # so gating them means a home-screen tile with no picture on it.
                 # They were reachable only by accident before: a branded install
                 # got the (public) /brand/asset/pwa-* sizes instead, and an
                 # UNBRANDED one — the default — quietly 303'd to /setup here.
                 # Now that the icon is always Ava's, this is the only path, so
                 # it has to work. Listed exactly, not by prefix, so the rest of
                 # the built bundle stays behind the login wall.
                 "/assets/icons/pwa-192.png", "/assets/icons/pwa-512.png",
                 "/assets/icons/pwa-maskable-512.png",
                 "/assets/icons/apple-touch-icon.png",
                 # The self-hosted typeface, for the same reason as the favicon:
                 # the sign-in, setup and claim cards are rendered before any
                 # cookie exists and they are styled in it (brand.pre_auth_css).
                 # Gated, these would 303 to /setup and the card would fall back
                 # to the OS default — the silent-fallback failure the whole
                 # self-hosting change exists to remove. Two static woff2 files
                 # leak strictly less than the accent, brand name and mark this
                 # page already serves unauthenticated. Listed exactly, not by
                 # prefix, per the note above.
                 "/fonts/inter-latin-wght-normal.woff2",
                 "/fonts/inter-latin-wght-italic.woff2"}
# No /brand/asset/* is public any more. Those slots are the owner's logo and
# wordmark, which now appear only INSIDE the signed-in app — the sign-in card
# renders Ava's mark inline (brand.pre_auth_mark), so nothing pre-auth fetches
# one, and leaving them reachable would publish an owner's artwork to anyone who
# could reach the port.

# Path prefixes that should answer 401 JSON (API-shaped) rather than redirect
# to /login when unauthenticated. Overlay route modules may append their own
# prefixes at register() time (same extension pattern as internal._TOKEN_GROUPS
# and access_policy._PROJECT_DENY).
API_PREFIXES = ["/api", "/apps", "/uploads"]


def _is_ingest(path: str) -> bool:
    """The inbound device-event channel (POST /api/connectors/<id>/events). Callers
    are third-party apps, not browsers, so this bypasses the cookie gate and does
    its own per-connector bearer check (see ava_bridge/internal.verify_ingest)."""
    return path.startswith("/api/connectors/") and path.endswith("/events")


# ---- DNS rebinding ----------------------------------------------------------- #
# A page at attacker-controlled DNS whose TTL expires and re-resolves to 127.0.0.1
# can reach this bridge from the owner's own browser. Measured before this existed:
#
#   may_claim  peer=127.0.0.1  Host=evil.example.com:8096  -> True
#
# so an unclaimed bare-metal instance could have its admin password set by a page
# the owner merely visited.
#
# **`samesite="lax"` does not help.** After rebinding, the page and the target are
# the SAME ORIGIN as far as the browser is concerned, so the session cookie is sent
# and every authenticated route is reachable too. That is why the defence has to be
# a Host allowlist and not a cookie attribute — the browser cannot tell the
# difference, but the server can: no legitimate client asks for Ava under a name
# Ava does not answer to.
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]",
                              "0.0.0.0", "host.docker.internal"})


def trusted_hosts() -> frozenset[str]:
    """Host header values this bridge answers to.

    Loopback names, whatever `server.host` and `server.public_url` name, plus any
    `server.trusted_hosts` the owner declares — a reverse proxy or a tailnet name
    is a legitimate deployment, and the escape hatch is what keeps this defence
    from being the thing people disable.

    `server.trusted_hosts: ["*"]` opts out entirely. It is honoured, because an
    owner who needs it should not have to patch the code, and refused silence is
    worse than a documented switch.
    """
    from . import config, settings

    out = set(_LOCAL_HOSTNAMES)
    for v in (config.SERVER_HOST, ):
        if v:
            out.add(str(v).strip().lower())
    pub = str(getattr(config, "PUBLIC_URL", "") or "")
    if "//" in pub:
        out.add(pub.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[0].lower())
    extra = settings.get("server.trusted_hosts", []) or []
    if isinstance(extra, str):
        extra = [extra]
    for v in extra:
        out.add(str(v).strip().lower())
    return frozenset(o for o in out if o)


def host_is_trusted(request) -> tuple[bool, str]:
    """(ok, reason). Compares the Host header's NAME, port stripped."""
    allowed = trusted_hosts()
    if "*" in allowed:
        return True, "server.trusted_hosts includes '*'"
    raw = (request.headers.get("host") or "").strip().lower()
    if not raw:
        # HTTP/1.1 requires Host. Absent means a hand-rolled client, not a browser,
        # and the rebinding threat model is browser-only — so allow it rather than
        # break curl and the health probes.
        return True, "no Host header"
    # Host is either `name[:port]` or `[v6addr][:port]`. Splitting on the last
    # colon breaks bare IPv6, and stripping brackets after splitting breaks the
    # bracketed form — `[::1]:8096` came out as `::1]:8096` and a legitimate IPv6
    # loopback was refused.
    if raw.startswith("[") and "]" in raw:
        name = raw[1:raw.index("]")]
    elif raw.count(":") == 1:
        name = raw.rsplit(":", 1)[0]
    else:
        name = raw
    if name in allowed:
        return True, ""
    return False, (
        f"this bridge does not answer to Host {raw!r}. If that is a reverse proxy "
        "or a VPN name you use, add it to `server.trusted_hosts` in ava.yaml. "
        "Refusing because a Host Ava does not recognise is the signature of DNS "
        "rebinding: a page on someone else's domain, re-resolved to this machine, "
        "reaching Ava from your own browser with your own cookie.")

def _shell_bounce(request: Request, path: str):
    """Bounce a TOP-LEVEL visit to an app URL into the Ava shell, or None.

    `phone_bridge.app_ui_proxy` already does this for the single-origin case:
    someone who types or bookmarks `/apps/<id>/` lands in the bare app with no
    Ava around it, so it redirects to `/#<id>`. With `apps.origin` configured
    that line is unreachable — the origin split answers first, and the owner
    gets `{"error":"forbidden","detail":"no embed token"}` as a raw JSON page.
    Measured, both directions: 403 on the apps host, 404 on the main one. The
    stranding the redirect exists to prevent came back as soon as the feature
    that needed it most was turned on.

    Only for a top-level navigation. The iframe load itself sends
    `Sec-Fetch-Dest: iframe` and must keep getting the 403 — that refusal IS the
    boundary. `fetch()` from app JS sends `empty` and is likewise untouched.

    The target is `server.public_url`, which the owner configured; nothing from
    the request reaches it, so this cannot become an open redirect. The cid is
    charset-checked before it goes in the fragment.
    """
    if request.method != "GET":
        return None
    if request.headers.get("sec-fetch-dest") != "document":
        return None
    from . import apps_origin as _apps_origin
    if not _apps_origin.configured() or not _apps_origin.is_app_path(path):
        return None
    cid = _apps_origin.cid_from_path(path) or ""
    if not cid or not all(c.isalnum() or c in "-_" for c in cid):
        return None
    return RedirectResponse(f"{config.PUBLIC_URL.rstrip('/')}/#{cid}",
                            status_code=302)


async def auth_gate(request: Request, call_next):
    path = request.url.path
    # HOST ALLOWLIST, before everything — including the origin split and
    # /internal/*. A rebound request is same-origin to the browser, so no cookie
    # attribute and no Origin check can see it; only the server noticing it was
    # asked for under a name it does not answer to. See trusted_hosts().
    _host_ok, _host_why = host_is_trusted(request)
    if not _host_ok:
        return JSONResponse({"error": "untrusted host", "detail": _host_why},
                            status_code=421)
    # ORIGIN SPLIT, before anything else including the /internal/* branch.
    #
    # When `apps.origin` is set, connector app UIs are served from a second
    # hostname so their JavaScript is cross-origin with Ava: no session cookie, no
    # `parent` access. That is only worth anything if the apps host ALSO cannot
    # reach Ava's own routes, so this refuses in both directions — `/apps/*` off
    # the apps host, and everything-but-`/apps/*` on it.
    #
    # It sits above the auth checks deliberately: the point is that the apps origin
    # has no session at all, so "is this caller authed" is the wrong question there.
    # See ava_bridge/apps_origin.py for why an Origin header check cannot do this.
    from . import apps_origin as _apps_origin
    _why = _apps_origin.refuses(request, path)
    if _why is not None:
        _bounce = _shell_bounce(request, path)
        if _bounce is not None:
            return _bounce
        return JSONResponse({"error": "wrong origin", "detail": _why},
                            status_code=404)
    if _apps_origin.configured() and _apps_origin.on_apps_host(request):
        # Reached only for /apps/* (refuses() sent everything else away). There is
        # no session here by design, so the embed token is the whole gate.
        _ok, _set, _reason = _apps_origin.authorize(request, path)
        if not _ok:
            _bounce = _shell_bounce(request, path)
            if _bounce is not None:
                return _bounce
            return JSONResponse({"error": "forbidden", "detail": _reason},
                                status_code=403)
        _resp = await call_next(request)
        if _set:
            _apps_origin.apply_cookie(_resp, _apps_origin.cid_from_path(path), _set,
                                      secure=request.url.scheme == "https")
        return _resp
    # /internal/* is the sandbox-tool callback surface; per-route handlers do
    # the SCOPED check (ava_bridge/internal.authorized), but the token must be
    # valid before the request reaches routing at all — otherwise FastAPI's
    # parameter validation answers unauthenticated callers (422s that leak the
    # route's schema) ahead of any auth check.
    if path == "/internal" or path.startswith("/internal/"):
        from . import config as _config, internal
        tok = request.headers.get("x-ava-internal-token", "")
        group = internal._token_group(tok) if (_config.INTERNAL_TOKEN and tok) else None
        if group is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Least privilege, enforced HERE rather than per handler. 24 of 25
        # handlers passed no scope, so `authorized(scope=)` was documented in
        # SECURITY.md, agent/install.sh and this file while being enforced
        # nowhere: any valid group token reached every route, including
        # /internal/code-change from the group that runs web_fetch. Checking in
        # the middleware means a route added tomorrow is covered by default —
        # and an unclassified one is refused rather than silently open.
        if not internal.group_may(group, path):
            return JSONResponse(
                {"error": "forbidden",
                 "detail": f"the '{group}' capability group may not call {path}"},
                status_code=403)
        # Attributed to the AGENT: /internal/* is the sandbox-tool callback
        # surface, so anything recorded downstream of here was done by Ava acting,
        # not by the owner clicking. That distinction is the whole point of the
        # audit `actor` field — "did I do this, or did my agent?" was previously
        # unanswerable for every event in the ledger.
        from . import audit as _audit
        _tok = _audit.set_actor("agent")
        try:
            return await call_next(request)
        finally:
            _audit.reset_actor(_tok)
    # The device-event ingest endpoint bypasses the cookie gate the same way:
    # callers are apps, not browsers, with their own per-connector bearer check.
    if path in _PUBLIC_PATHS or _is_ingest(path) or is_authed(request):
        # A cookie-authenticated request is the OWNER; a public path or a device
        # ingest is neither owner nor agent, so it stays unattributed rather than
        # being labelled with a plausible guess.
        from . import audit as _audit
        _tok = _audit.set_actor("owner" if is_authed(request) else "unknown")
        try:
            return await call_next(request)
        finally:
            _audit.reset_actor(_tok)
    if any(path == p or path.startswith(p + "/") for p in API_PREFIXES):
        return JSONResponse({"error": "auth required"}, status_code=401)
    # First run (no password yet) -> onboarding screen, not a dead login wall.
    return RedirectResponse("/setup" if needs_setup() else "/login", status_code=303)
