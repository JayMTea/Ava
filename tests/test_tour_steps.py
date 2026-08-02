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
SRC = ROOT / "frontend" / "src"
STEPS = SRC / "components" / "tour" / "steps.ts"
APP = SRC / "App.tsx"
METRICS = SRC / "components" / "dashboard" / "metrics.ts"
OVERVIEW = SRC / "components" / "hub" / "panels" / "Overview.tsx"
VITALS = SRC / "components" / "dashboard" / "VitalsView.tsx"
BACKEND = ROOT / "ava_bridge" / "hub" / "tour.py"


def _frontend_sources() -> str:
    """Every .tsx in the SPA, concatenated. Anchors are scattered across views by
    design — the point of data-tour is that a step can point anywhere."""
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(SRC.rglob("*.tsx")))


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


def test_every_data_tour_anchor_a_step_points_at_exists() -> None:
    """A step whose selector matches nothing is not an error at runtime — it
    degrades to a centered card (tour/Spotlight.tsx says so deliberately). That
    graceful fallback is exactly why an unhooked anchor is INVISIBLE: the
    walkthrough still runs, still reads correctly, and simply stops highlighting.
    Nothing else would catch a rename."""
    anchors = set(re.findall(r'data-tour="([\w-]+)"', _frontend_sources()))
    # The Setup cards build their anchor from the tab id: data-tour={`hub-${t}`}.
    anchors |= {f"hub-{t}" for t in
                re.findall(r"card\('(\w+)'", OVERVIEW.read_text(encoding="utf-8"))}
    # <Panel tour="x"> forwards to data-tour on the section (dashboard/layout.tsx).
    anchors |= set(re.findall(r'\btour="([\w-]+)"', _frontend_sources()))
    for target in re.findall(r'\[data-tour="([\w-]+)"\]', STEPS.read_text(encoding="utf-8")):
        assert target in anchors, (
            f"a walkthrough step points at [data-tour={target!r}], which nothing "
            "in frontend/src renders. The step will still show, just with nothing "
            "highlighted — add the anchor back or repoint the step.")


def test_the_vitals_budget_panel_is_not_hidden_until_a_cap_is_set() -> None:
    """The bug this exists for: the panel used to render only once a cap was
    configured, and a cap is unset by default (dashboard.py reads it with a bare
    .get, and nothing seeds one at install). So the one user the walkthrough is
    built for — a brand new one — was the one user guaranteed to reach the
    "Spend and energy" step with the panel absent and nothing highlighted.
    The budgets panel already learned this (hub/panels/BudgetsPanel.tsx: "Unlike the
    old bar it ALWAYS renders"); Vitals must not drift back."""
    body = VITALS.read_text(encoding="utf-8")
    panel = body.rindex("<Panel", 0, body.index('tour="vitals-budget"'))
    guard = body.rindex("{budget.data", 0, panel)
    assert body[guard:panel].strip() == "{budget.data && (", (
        "the Vitals budget panel is gated on something beyond /api/budget having "
        "answered. If that gate is a configured cap, a fresh install sees no "
        "spend or energy at all and the walkthrough highlights nothing.")


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
