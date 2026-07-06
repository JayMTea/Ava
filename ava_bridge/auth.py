"""Authentication: password gate, signed-cookie sessions, login throttle, middleware.

Step-zero gate for the Experience plane (:8445). The Tailnet already limits *who*
can reach this port; this adds a *something-you-know* factor so a logged-in device
on the tailnet still can't drive Ava without the password. A single shared password
issues a signed (HMAC) session cookie. Stateless: survives restarts, no server-side
session store to leak.
"""
import hashlib
import hmac
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


_AUTH_SECRET = _auth_secret()


def _make_token() -> str:
    exp = str(int(time.time()) + config.SESSION_TTL)
    sig = hmac.new(_AUTH_SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _valid_token(tok: str) -> bool:
    if not tok or "." not in tok:
        return False
    exp, _, sig = tok.partition(".")
    good = hmac.new(_AUTH_SECRET, exp.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        return int(exp) > int(time.time())
    except ValueError:
        return False


def _is_authed(request: Request) -> bool:
    return _valid_token(request.cookies.get(config.COOKIE_NAME, ""))


def _set_session_cookie(resp: Response) -> None:
    resp.set_cookie(config.COOKIE_NAME, _make_token(), max_age=config.SESSION_TTL,
                    httponly=True, secure=config.COOKIE_SECURE, samesite="lax", path="/")


def _login_locked(ip: str) -> bool:
    with state.login_lock:
        rec = state.login_fails.get(ip)
        if not rec:
            return False
        count, first = rec
        if time.time() - first > config.LOGIN_WINDOW:
            state.login_fails.pop(ip, None)
            return False
        return count >= config.LOGIN_MAX


def _login_record(ip: str, ok: bool) -> None:
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


_PUBLIC_PATHS = {"/login", "/logout", "/setup", "/api/health", "/favicon.ico"}


def _is_ingest(path: str) -> bool:
    """The inbound device-event channel (POST /api/connectors/<id>/events). Callers
    are third-party apps, not browsers, so this bypasses the cookie gate and does
    its own per-connector bearer check (see ava_bridge/internal.verify_ingest)."""
    return path.startswith("/api/connectors/") and path.endswith("/events")


async def auth_gate(request: Request, call_next):
    path = request.url.path
    # /internal/* is the sandbox-tool callback surface; it enforces its own
    # bearer-token check (see ava_bridge/internal.py), so it bypasses the
    # cookie gate rather than redirecting to /login. The device-event ingest
    # endpoint bypasses the same way for the same reason (its own bearer check).
    if (path in _PUBLIC_PATHS or path.startswith("/internal") or _is_ingest(path)
            or _is_authed(request)):
        return await call_next(request)
    if path.startswith("/api") or path.startswith("/studio") \
            or path.startswith("/apps") \
            or path.startswith("/media") or path.startswith("/uploads"):
        return JSONResponse({"error": "auth required"}, status_code=401)
    # First run (no password yet) -> onboarding screen, not a dead login wall.
    return RedirectResponse("/setup" if needs_setup() else "/login", status_code=303)
