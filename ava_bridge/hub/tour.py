"""Setup -> the first-run walkthrough: which pages the owner has already seen.

State lives HERE rather than in the browser, and that is the whole design
decision. Ava is single-user by construction — one admin password per install,
no user table (docs/PACKAGING_PLAN.md:15) — so "per user" and "per install" are
the same thing, and ava.yaml is a legitimate place for it. But Ava is also an
installable PWA that the owner opens from a phone as well as a desktop, and
localStorage is per-browser-profile: a client-side flag would replay the whole
walkthrough on the second device and after any cache clear, which is exactly the
"returning user" case the feature exists to suppress.

A LIST of page ids, not a single boolean, because Skip dismisses one page rather
than all of them: someone who never opens Data must not have its walkthrough
marked seen on their behalf.

An install with no key recorded has seen nothing, so an existing owner upgrading
into this version gets the walkthrough once. That is deliberate — the alternative
is back-filling every install as "seen", which silently denies the guidance to
precisely the people who have been using an unexplained UI the longest.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import settings

router = APIRouter()

_KEY = "ui.tours_seen"

# The pages that have a walkthrough. Server-side allowlist, following the same
# per-route validation convention as hub/cost.py and hub/persona.py: the client
# names a page, this decides whether that is a page. Without it, a typo (or
# anything else posting here) writes junk into ava.yaml that nothing ever reads
# and nothing ever cleans up.
PAGES = ("hub", "chat", "vitals", "ops", "data")


def seen() -> list[str]:
    """Pages already walked through.

    Read through settings at REQUEST time rather than snapshotted into config.py
    at import — settings.py loads the config once at import, so a module constant
    would need a bridge restart before a dismissal took effect, and "I clicked
    Skip and it came back" is the one bug this route cannot afford.
    """
    raw = settings.get(_KEY, []) or []
    if not isinstance(raw, list):
        return []
    return [p for p in (str(x).strip() for x in raw) if p in PAGES]


@router.get("/tour")
def tour_get():
    """What the SPA needs to decide whether to start a page's walkthrough."""
    return {"seen": seen(), "pages": list(PAGES)}


@router.post("/tour/seen")
def tour_seen(page: str = ""):
    """Mark one page walked through. Idempotent — a double-submit is not an error."""
    page = (page or "").strip()
    if page not in PAGES:
        return JSONResponse(
            {"ok": False, "error": f"unknown page {page!r}"}, status_code=400)
    current = seen()
    if page not in current:
        current.append(page)
        settings.save_patch({"ui": {"tours_seen": current}})
    # No restart_required: seen() reads at request time, so this is already live.
    return {"ok": True, "seen": current}


@router.post("/tour/reset")
def tour_reset():
    """Replay: forget every page, so the walkthrough runs again from the start."""
    settings.save_patch({"ui": {"tours_seen": []}})
    return {"ok": True, "seen": []}
