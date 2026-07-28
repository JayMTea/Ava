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

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import config, state


def _secure_opener(path: str, flags: int) -> int:
    """open() opener that creates new files 0600 so secrets never briefly exist
    world-readable between create and chmod."""
    return os.open(path, flags, 0o600)


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
    if not hmac.compare_digest(sig, _sign(f"{exp}.{sid}")):
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
# own console. Same shape as Jupyter's token and Home Assistant's onboarding.

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
    if _is_loopback(client_ip(request)):
        return True
    want = claim_token()
    if not want:
        return False
    got = (request.query_params.get("claim")
           or request.headers.get("x-ava-setup-claim") or "")
    return bool(got) and hmac.compare_digest(got.strip(), want)


def claim_hint() -> str:
    """What to tell a remote caller who has no token. Names the file rather than
    the value: the point is that you must be able to read the server's disk."""
    return (f"Run this on the machine Ava is installed on:\n"
            f"    cat {_CLAIM_FILE}\n"
            f"then open  <this-url>/setup?claim=<token>")


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
                 # PWA shell: browsers fetch the manifest and service worker
                 # without credentials context — they must not bounce to /login.
                 "/manifest.webmanifest", "/sw.js"}

# Path prefixes that should answer 401 JSON (API-shaped) rather than redirect
# to /login when unauthenticated. Overlay route modules may append their own
# prefixes at register() time (same extension pattern as internal._TOKEN_GROUPS
# and access_policy._PROJECT_DENY).
API_PREFIXES = ["/api", "/apps", "/media", "/uploads", "/thumb"]


def _is_ingest(path: str) -> bool:
    """The inbound device-event channel (POST /api/connectors/<id>/events). Callers
    are third-party apps, not browsers, so this bypasses the cookie gate and does
    its own per-connector bearer check (see ava_bridge/internal.verify_ingest)."""
    return path.startswith("/api/connectors/") and path.endswith("/events")


async def auth_gate(request: Request, call_next):
    path = request.url.path
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
        return await call_next(request)
    # The device-event ingest endpoint bypasses the cookie gate the same way:
    # callers are apps, not browsers, with their own per-connector bearer check.
    if path in _PUBLIC_PATHS or _is_ingest(path) or is_authed(request):
        return await call_next(request)
    if any(path == p or path.startswith(p + "/") for p in API_PREFIXES):
        return JSONResponse({"error": "auth required"}, status_code=401)
    # First run (no password yet) -> onboarding screen, not a dead login wall.
    return RedirectResponse("/setup" if needs_setup() else "/login", status_code=303)
