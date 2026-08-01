"""The walkthrough's page ids must agree across three files that cannot see
each other, and its copy must not fork the metric glossary.

The id agreement is not cosmetic. A page the frontend tours but the backend
rejects can never be recorded as seen — so it would run on every single visit,
forever, and the only symptom is a user saying "it keeps coming back".

Static scan, in the style of tests/test_diagram_sync.py: reads tracked files,
needs no bridge, no node and no browser.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
STEPS = ROOT / "frontend" / "src" / "components" / "tour" / "steps.ts"
APP = ROOT / "frontend" / "src" / "App.tsx"
METRICS = ROOT / "frontend" / "src" / "components" / "dashboard" / "metrics.ts"
BACKEND = ROOT / "ava_bridge" / "hub" / "tour.py"


def _tour_pages() -> list[str]:
    body = STEPS.read_text(encoding="utf-8")
    block = body[body.index("export const TOURS"):]
    return re.findall(r"^  (\w+): \[", block, re.M)


def test_every_toured_page_is_a_real_view() -> None:
    views = set(re.search(r"BUILTIN_VIEWS = \[([^\]]*)\]",
                          APP.read_text(encoding="utf-8")).group(1).replace("'", "").replace(" ", "").split(","))
    for page in _tour_pages():
        assert page in views, (
            f"the walkthrough tours {page!r}, which is not a view in "
            "BUILTIN_VIEWS — it can never be reached")


def test_the_backend_accepts_every_page_the_frontend_tours() -> None:
    """The failure this exists for: a page the frontend tours and the backend
    refuses to record is a walkthrough that runs on every visit, forever."""
    allowed = set(re.search(r'PAGES = \(([^)]*)\)',
                            BACKEND.read_text(encoding="utf-8")).group(1)
                  .replace('"', "").replace(" ", "").replace("\n", "").split(","))
    for page in _tour_pages():
        assert page in allowed, (
            f"steps.ts tours {page!r} but ava_bridge/hub/tour.py PAGES does not "
            "accept it, so dismissing it would 400 and it would show again")


def test_the_landing_page_is_setup() -> None:
    """A bare `/` is where the setup wizard's location.href='/' lands, and the
    answer to "no opinion" is the page that helps you set the product up."""
    app = APP.read_text(encoding="utf-8")
    assert "viewFromHash() || 'hub'" in app, (
        "the no-hash landing is no longer Setup")
    # Match the CALL, not the string: the comment above that code names the key
    # to explain why it is gone, and a bare substring check would fail on the
    # explanation for the very thing it is checking.
    for call in ("getItem('ava.view')", "setItem('ava.view'"):
        assert call not in app, (
            f"localStorage {call} is back. It made the first visit's default a "
            "sticky preference the user never chose, which is how landing on "
            "Vitals became permanent for anyone who opened the app once.")


def test_the_walkthrough_does_not_fork_the_metric_glossary() -> None:
    """metrics.ts is the one place a metric is explained, reused by every ⓘ and
    promised as single-source in docs/capabilities/vitals.md. The Vitals step
    teaches the ⓘ affordance instead of restating it; a second copy here would
    drift and there would be no way to tell which was right."""
    glossary = re.findall(r"'((?:[^'\\]|\\.){40,})'", METRICS.read_text(encoding="utf-8"))
    steps = STEPS.read_text(encoding="utf-8")
    for entry in glossary:
        sentence = entry.split(".")[0].strip()
        if len(sentence) < 40:
            continue
        assert sentence not in steps, (
            f"a walkthrough step restates the glossary: {sentence[:60]!r}. Point "
            "at the ⓘ instead — one explanation, one place.")
