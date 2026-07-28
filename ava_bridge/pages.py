"""Server-rendered pages and the SPA shell — the routes a browser hits directly.

Login, first-run setup, logout, password change, the SPA index, and the PWA
assets (manifest, service worker, favicon), plus /legacy for the single-file UI
kept reachable during the strangler-fig migration to the React app.

This module also OWNS the page assets — the three externalised HTML templates
and the built SPA index — because it is the only place that renders them. One
exception, and it is why phone_bridge.py imports this module before it builds
the app: `app.mount("/assets", ...)` is app construction, so it stays there,
but the directory and the "did the SPA get built" check live here. phone_bridge
reads pages.SPA_PAGE / pages.FRONTEND_DIST for that mount rather than keeping a
second copy of the same two facts.

Auth note: these routes are what the auth gate exists to protect, so several of
them call into ava_bridge/auth.py. Those functions were named with a leading
underscore while being imported by four call sites here; they are public now,
because an underscore that another module reaches past is not a private
contract.
"""
import hmac
import html
import json
import os
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)

from . import auth, config
from .auth import (claim_hint, clear_claim, client_ip, current_password,
                   is_authed, login_locked, login_record, may_claim,
                   rotate_secret, set_password,
                   set_session_cookie)
from .config import COOKIE_NAME

# ---- Web templates (externalised to ava_bridge/web/*.html) -------------------
with open(os.path.join(config.WEB_DIR, "index.html"), encoding="utf-8") as _f:
    LEGACY_PAGE = _f.read()
with open(os.path.join(config.WEB_DIR, "login.html"), encoding="utf-8") as _f:
    LOGIN_PAGE = _f.read().replace("Ava", config.AVA_NAME)
with open(os.path.join(config.WEB_DIR, "setup.html"), encoding="utf-8") as _f:
    SETUP_PAGE = _f.read().replace("__BRAND__", config.AVA_NAME)

# ---- New Vite + React SPA (frontend/dist) -----------------------------------
# The primary UI is the built SPA. The single-file legacy UI stays reachable at
# /legacy so nothing breaks while views are ported (strangler-fig migration).
FRONTEND_DIST = os.path.join(config.ROOT, "frontend", "dist")
_spa_index_path = os.path.join(FRONTEND_DIST, "index.html")
if os.path.isfile(_spa_index_path):
    with open(_spa_index_path, encoding="utf-8") as _f:
        SPA_PAGE = _f.read()
else:
    SPA_PAGE = None

def _claim_html() -> str:
    """The claim instructions, as one HTML line. Escaped because it embeds a
    filesystem path that comes from AVA_HOME."""
    return ("Read the claim token on the machine Ava runs on:<br>"
            f"<code>{html.escape(claim_hint().splitlines()[1].strip())}</code><br>"
            "then reload this page with <code>?claim=&lt;token&gt;</code>.")


router = APIRouter()

# --- Authentication routes ---------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if is_authed(request):
        return RedirectResponse("/", status_code=303)
    if auth.needs_setup():   # no password yet -> first-run onboarding, not a login wall
        return RedirectResponse("/setup", status_code=303)
    return HTMLResponse(LOGIN_PAGE.replace("<!--MSG-->", ""))

@router.post("/login")
def login_post(request: Request, password: str = Form("")):
    ip = client_ip(request) or "?"
    if login_locked(ip):
        return HTMLResponse(
            LOGIN_PAGE.replace("<!--MSG-->", "Too many attempts &mdash; wait a minute."),
            status_code=429)
    pw = current_password()
    if pw and hmac.compare_digest(password, pw):
        login_record(ip, ok=True)
        resp = RedirectResponse("/", status_code=303)
        set_session_cookie(resp, request)
        return resp
    login_record(ip, ok=False)
    time.sleep(0.5)
    return HTMLResponse(
        LOGIN_PAGE.replace("<!--MSG-->", "Incorrect password."), status_code=401)

@router.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    """First-run: create the admin password. Only reachable until one is set."""
    if not auth.needs_setup():
        return RedirectResponse("/login", status_code=303)
    if not may_claim(request):
        return HTMLResponse(
            SETUP_PAGE.replace(
                "<!--MSG-->",
                "This Ava has not been claimed yet, and you are not connecting "
                "from the machine it runs on. " + _claim_html()),
            status_code=403)
    return HTMLResponse(SETUP_PAGE.replace("<!--MSG-->", ""))

@router.post("/setup")
def setup_post(request: Request, password: str = Form(""), confirm: str = Form("")):
    if not auth.needs_setup():   # password already set -> can't reset via this screen
        return RedirectResponse("/login", status_code=303)
    # An unclaimed instance bound off-loopback is otherwise first-come-first-served:
    # whoever on the network reaches it first sets the admin password and the owner
    # is locked out of their own box. Loopback callers are trusted; everyone else
    # proves they can read the server's disk.
    if not may_claim(request):
        return HTMLResponse(
            SETUP_PAGE.replace("<!--MSG-->", "Setup requires the claim token. " + _claim_html()),
            status_code=403)
    password = (password or "").strip()
    if len(password) < 8:
        return HTMLResponse(
            SETUP_PAGE.replace("<!--MSG-->", "Password must be at least 8 characters."),
            status_code=400)
    if password != (confirm or "").strip():
        return HTMLResponse(
            SETUP_PAGE.replace("<!--MSG-->", "Passwords do not match."), status_code=400)
    set_password(password)
    clear_claim()       # the window is over; the instance now has an owner
    # Fresh install -> continue into the onboarding wizard (hardware, backend,
    # features, connectors). A pre-existing config skips it (setup_completed()).
    from ava_bridge.setup_wizard import setup_completed
    dest = "/" if setup_completed() else "/setup/wizard"
    resp = RedirectResponse(dest, status_code=303)
    set_session_cookie(resp, request)
    return resp

@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

@router.post("/api/auth/password")
async def change_password(request: Request):
    """Change the admin password from inside the product, revoking other sessions.

    Until this existed the only ways to change it were editing data/auth_password
    by hand or setting AVA_PASSWORD and restarting — neither reachable by someone
    running Ava as an app rather than as their own repo.

    Rotating the signing secret is what revokes: there is no server-side session
    store, so a stolen or stale cookie is only invalidated by changing the key
    that signs it. The caller is re-issued a fresh cookie so changing your own
    password does not log you out of the tab you did it from.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a malformed body is a 400, not a 500
        return JSONResponse({"ok": False, "error": "expected a JSON body"},
                            status_code=400)
    current = str(body.get("current") or "")
    new = str(body.get("new") or "").strip()
    if len(new) < 8:
        return JSONResponse(
            {"ok": False, "error": "New password must be at least 8 characters."},
            status_code=400)
    # compare_digest, not ==, so a wrong guess costs the same time as a right one.
    if not hmac.compare_digest(current, current_password()):
        return JSONResponse({"ok": False, "error": "Current password is incorrect."},
                            status_code=403)
    if os.environ.get("AVA_PASSWORD"):
        return JSONResponse(
            {"ok": False, "error": "AVA_PASSWORD is set in the environment and "
                                   "takes precedence — change it there instead."},
            status_code=409)
    set_password(new)
    revoked = rotate_secret()
    resp = JSONResponse({"ok": True, "revoked_other_sessions": revoked})
    set_session_cookie(resp, request)
    return resp

@router.get("/", response_class=HTMLResponse)
def index():
    # Serve index.html fresh from disk so a frontend rebuild (which changes the
    # hashed asset filenames) is picked up immediately — without this, a cached
    # page can reference a bundle that the rebuild deleted, giving a black screen.
    if SPA_PAGE is not None:
        try:
            with open(_spa_index_path, encoding="utf-8") as _f:
                return _f.read()
        except OSError:
            return SPA_PAGE
    return LEGACY_PAGE

# PWA shell files live at the dist ROOT (not dist/assets/), so the /assets
# mount doesn't cover them. Served explicitly; both are in auth._PUBLIC_PATHS.
@router.get("/manifest.webmanifest", include_in_schema=False)
def pwa_manifest():
    """The PWA manifest, with the brand applied at request time.

    vite bakes `name`/`short_name` into dist at BUILD time, so a fork that set
    `brand.name` in ava.yaml still got a home-screen icon labelled "Ava" — the one
    place branding is most visible and least expected to be wrong. Overriding here
    keeps the built file as the template and the config as the source of truth,
    with no rebuild required to re-brand.
    """
    p = os.path.join(FRONTEND_DIST, "manifest.webmanifest")
    if not os.path.isfile(p):
        return JSONResponse({"error": "not built"}, status_code=404)
    try:
        with open(p, encoding="utf-8") as f:
            man = json.load(f)
        man["name"] = config.AVA_NAME
        man["short_name"] = config.AVA_NAME
        if config.AVA_TAGLINE:
            man["description"] = config.AVA_TAGLINE
        return JSONResponse(man, media_type="application/manifest+json")
    except (OSError, ValueError):
        # A malformed or unreadable manifest must not break install; serve the
        # built file untouched rather than 500.
        return FileResponse(p, media_type="application/manifest+json")

@router.get("/sw.js", include_in_schema=False)
def pwa_sw():
    p = os.path.join(FRONTEND_DIST, "sw.js")
    if not os.path.isfile(p):
        return JSONResponse({"error": "not built"}, status_code=404)
    # no-cache: the worker itself must always revalidate, or updates stall.
    return FileResponse(p, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})

@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    p = os.path.join(FRONTEND_DIST, "favicon.ico")
    if not os.path.isfile(p):
        return JSONResponse({"error": "not built"}, status_code=404)
    return FileResponse(p, media_type="image/x-icon")

@router.get("/legacy", response_class=HTMLResponse)
def legacy_index():
    return LEGACY_PAGE

