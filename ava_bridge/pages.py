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
import html
import json
import os
import time

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)

from . import auth, brand, config, settings
from .auth import (claim_read_cmd, claim_windows_note, clear_claim, client_ip,
                   current_password, is_authed, login_locked, login_record,
                   may_claim, rotate_secret, set_password,
                   set_session_cookie)
from .config import COOKIE_NAME
from .security import constant_time_equals

# ---- Web templates (externalised to ava_bridge/web/*.html) -------------------
with open(os.path.join(config.WEB_DIR, "index.html"), encoding="utf-8") as _f:
    LEGACY_PAGE = _f.read()
# Held UNSUBSTITUTED. These used to be branded at import — `LOGIN_PAGE` by a
# blanket `.read().replace("Ava", config.AVA_NAME)` — which had three defects:
#
#   1. A substring replace over the whole document. It worked only because
#      login.html happened to contain no other word starting "Ava"; the first
#      time anyone wrote "Available" in that page, the corporate name would have
#      been spliced through the middle of it.
#   2. It ran once, at import, so a brand saved from Setup needed a full restart
#      to reach the sign-in page — which is precisely the restart the Branding
#      panel is designed not to need.
#   3. It disagreed with setup.html, which already used a `__BRAND__` sentinel.
#
# All three pages now carry sentinels and are rendered per request by _render().
with open(os.path.join(config.WEB_DIR, "login.html"), encoding="utf-8") as _f:
    LOGIN_TMPL = _f.read()
with open(os.path.join(config.WEB_DIR, "setup.html"), encoding="utf-8") as _f:
    SETUP_TMPL = _f.read()
with open(os.path.join(config.WEB_DIR, "claim.html"), encoding="utf-8") as _f:
    CLAIM_TMPL = _f.read()

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

def _render(tmpl: str, msg: str = "") -> str:
    """Thin alias for brand.render_page — kept so the four call sites below
    read the same as they always did."""
    return brand.render_page(tmpl, msg)


def _render_claim(msg: str = "") -> str:
    """The claim step: a page of its own, not a red line over the password form.

    Serving setup.html to a caller who cannot claim renders a complete, focused,
    submittable password form that POST /setup is guaranteed to refuse — so the
    honest reading of that screen ("fill this in") is the one thing that cannot
    work. Ask for the one input that CAN move them forward instead.

    The form is a GET at /setup with a field named `claim`, which is exactly the
    URL shape `may_claim()` already accepts. No new way in, no second code path
    to keep in step with the gate: the browser builds the same link install.sh
    prints. Escaped because the command embeds a path derived from AVA_HOME.
    """
    return (_render(CLAIM_TMPL, msg)
            .replace("<!--CLAIM_CMD-->", html.escape(claim_read_cmd()))
            .replace("<!--CLAIM_NOTE-->", html.escape(claim_windows_note())))


router = APIRouter()

# --- Authentication routes ---------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if is_authed(request):
        return RedirectResponse("/", status_code=303)
    if auth.needs_setup():   # no password yet -> first-run onboarding, not a login wall
        return RedirectResponse("/setup", status_code=303)
    return HTMLResponse(_render(LOGIN_TMPL))

@router.post("/login")
def login_post(request: Request, password: str = Form("")):
    ip = client_ip(request) or "?"
    if login_locked(ip):
        return HTMLResponse(
            _render(LOGIN_TMPL, "Too many attempts &mdash; wait a minute."),
            status_code=429)
    pw = current_password()
    if pw and constant_time_equals(password, pw):
        login_record(ip, ok=True)
        resp = RedirectResponse("/", status_code=303)
        set_session_cookie(resp, request)
        return resp
    login_record(ip, ok=False)
    time.sleep(0.5)
    return HTMLResponse(
        _render(LOGIN_TMPL, "Incorrect password."), status_code=401)

@router.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    """First-run: create the admin password. Only reachable until one is set."""
    if not auth.needs_setup():
        return RedirectResponse("/login", status_code=303)
    if not may_claim(request):
        # Two different arrivals, and telling them apart is the whole point of
        # this branch. A BARE visit is the ordinary way to reach here on the
        # Docker path (the container sees the bridge gateway, never 127.0.0.1),
        # so it gets no error: nothing has gone wrong yet, and painting the first
        # screen of a fresh install red reads as a broken install, not a gate.
        # A visit CARRYING a claim that did not match is a real failure, and the
        # claim form submits right back here — so saying nothing would leave a
        # mistyped token looking identical to no token at all.
        tried = (request.query_params.get("claim")
                 or request.headers.get("x-ava-setup-claim") or "").strip()
        return HTMLResponse(
            _render_claim("That token was not accepted. Read it again below."
                          if tried else ""),
            status_code=403)
    return HTMLResponse(_render(SETUP_TMPL))

@router.post("/setup")
def setup_post(request: Request, password: str = Form(""), confirm: str = Form("")):
    if not auth.needs_setup():   # password already set -> can't reset via this screen
        return RedirectResponse("/login", status_code=303)
    # An unclaimed instance bound off-loopback is otherwise first-come-first-served:
    # whoever on the network reaches it first sets the admin password and the owner
    # is locked out of their own box. Loopback callers are trusted; everyone else
    # proves they can read the server's disk.
    if not may_claim(request):
        # The password form carries no claim, so a POST only lands here when the
        # gate closed between rendering that form and submitting it — someone
        # else claimed the instance, or the token was rotated. Say that, rather
        # than repeating "read the token", which is not what went wrong.
        return HTMLResponse(
            _render_claim("This instance was claimed while that form was open. "
                          "Read the current token below."),
            status_code=403)
    password = (password or "").strip()
    if len(password) < 8:
        return HTMLResponse(
            _render(SETUP_TMPL, "Password must be at least 8 characters."),
            status_code=400)
    if password != (confirm or "").strip():
        return HTMLResponse(
            _render(SETUP_TMPL, "Passwords do not match."), status_code=400)
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
    # Constant-time, not ==, so a wrong guess costs the same time as a right one.
    if not constant_time_equals(current, current_password()):
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
        man["name"] = brand.name()
        man["short_name"] = brand.name()
        if brand.tagline():
            man["description"] = brand.tagline()
        # The loop this docstring opened but never closed. Patching only the
        # NAME left a branded install with Ava's blue splash and Ava's icon on
        # the home screen — the two most visible pixels of a re-brand.
        if brand.icon_source():
            man["icons"] = [
                {"src": "/brand/asset/pwa-192", "sizes": "192x192", "type": "image/png"},
                {"src": "/brand/asset/pwa-512", "sizes": "512x512", "type": "image/png"},
                {"src": "/brand/asset/pwa-maskable-512", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ]
        if brand.chrome():
            man["theme_color"] = brand.chrome()
            man["background_color"] = brand.chrome()
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
    """The tab icon, branded when there is a brand.

    Serves a PNG under the .ico path when branded, which is correct: browsers
    honour the Content-Type, not the extension. Keeping the path unchanged means
    tests/_route_table.json is untouched and every existing <link rel="icon">
    keeps working.
    """
    branded = brand.derived_icon("favicon")
    if branded:
        return FileResponse(branded, media_type="image/png",
                            headers={"Cache-Control": "no-cache"})
    p = os.path.join(FRONTEND_DIST, "favicon.ico")
    if not os.path.isfile(p):
        return JSONResponse({"error": "not built"}, status_code=404)
    return FileResponse(p, media_type="image/x-icon")

@router.get("/brand/asset/{slot}", include_in_schema=False)
def brand_asset(slot: str, request: Request):
    """Serve one branding image.

    `slot` is a FIXED ENUM, not a filename — the five names in brand.SLOTS and
    nothing else. That is why there is no traversal defence here: there is no
    caller-supplied path to traverse with. The stored filename never appears in
    a URL, so the only copy of it stays on the server.

    Conditionally public. The sign-in page needs an <img> the browser fetches
    before any cookie exists, so when `brand.public` is true these paths are in
    auth._PUBLIC_PATHS; when it is false an unauthenticated caller gets the same
    404 an unset slot gives, which tells them nothing either way.

    404 rather than a placeholder image on an unset slot: a fallback would make
    "did my upload actually work?" unanswerable from the browser.
    """
    if slot not in brand.SLOTS and slot not in brand.DERIVED:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not brand.brand_is_public() and not is_authed(request):
        return JSONResponse({"error": "not found"}, status_code=404)
    if slot in brand.DERIVED:
        # Rendered from the one `icon` source (or the logo) on first request and
        # cached. These are what the PWA manifest points at.
        path = brand.derived_icon(slot)
        if not path:
            return JSONResponse({"error": "not found"}, status_code=404)
    else:
        fname = brand.asset_name(slot)
        if not fname:
            return JSONResponse({"error": "not found"}, status_code=404)
        path = os.path.join(settings.brand_dir(), fname)
        if not os.path.isfile(path):
            return JSONResponse({"error": "not found"}, status_code=404)
    st = os.stat(path)
    # A strong validator from (mtime_ns, size) so a re-upload is visible at once
    # while repeat loads are 304s. Deliberately NOT the `max-age=31536000,
    # immutable` that media_api uses for /thumb: that is right for a
    # content-addressed filename and wrong for a stable slot URL, where the
    # bytes behind the same path change every time the owner uploads.
    etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag,
                                                  "Cache-Control": "no-cache"})
    # Always PNG: ingest_asset re-encodes through Pillow, so whatever was
    # uploaded, what is stored and served is a PNG this process produced.
    ctype = "image/png"
    return FileResponse(path, media_type=ctype, headers={
        "ETag": etag,
        "Cache-Control": "no-cache",
        # The bytes are re-encoded by Pillow on ingest so they are known-good,
        # but nosniff costs nothing and keeps a future format addition from
        # turning into a content-type confusion bug.
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    })


@router.get("/legacy", response_class=HTMLResponse)
def legacy_index():
    return LEGACY_PAGE

