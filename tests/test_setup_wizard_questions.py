"""Convention guard: first run only asks what a first-run user can answer.

The wizard had a fourth step, "Enable connectors", offering pre-ticked
checkboxes for kind:core connectors — Ava's own HTTP bridge, the engine it
thinks with, and a router embedded in that bridge — so the only answer other
than "leave them alone" was self-harm, presented to someone who has been using
the product for ninety seconds. And the answer was inert: connectors.load()
filters on `enabled` in each MANIFEST and never reads the list that step wrote
to ava.yaml.

A first-run question earns its place only if there is no sensible default, the
user can actually answer it, and the answer changes something. That step failed
all three, which is the bar this guard encodes.

What was removed is the QUESTION, not the capability: /api/setup/connectors still
serves Setup -> Connectors in the SPA, where there is room to explain a connector
and where it appears once it actually exists on the machine.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
WIZARD = ROOT / "ava_bridge" / "web" / "setup_wizard.html"
WIZARD_PY = ROOT / "ava_bridge" / "setup_wizard.py"


def _html() -> str:
    return WIZARD.read_text(encoding="utf-8")


def test_first_run_does_not_ask_which_connectors_to_enable() -> None:
    html = _html()
    assert "/api/setup/connectors" not in html, (
        "the wizard is asking about connectors again. Three of them are "
        "kind:core — there is no answer but 'leave them alone' — and the one "
        "real option is not deployed outside the `full` profile.")
    assert "connList" not in html


def test_the_dots_match_the_steps() -> None:
    """The progress dots are hand-written, so a removed step leaves a dot that
    never fills — the user waits for a fourth screen that will never come."""
    html = _html()
    dots = len(re.findall(r'class="dot', html))
    steps = len(set(re.findall(r'data-step="(\d)"', html)))
    assert dots == steps, f"{dots} progress dots but {steps} steps"


def test_every_navigation_target_exists() -> None:
    html = _html()
    targets = {int(m) for m in re.findall(r"go\((\d)\)", html)}
    steps = {int(m) for m in re.findall(r'data-step="(\d)"', html)}
    assert targets <= steps, f"go() jumps to steps that do not exist: {targets - steps}"


def test_save_omits_connectors_rather_than_sending_an_empty_list() -> None:
    """save() built its list from the checkbox nodes. With those gone the
    selector returns [], and the save path patches ava.yaml whenever the key is
    PRESENT — so sending it would silently wipe the shipped `connectors:`
    default instead of leaving it alone."""
    html = _html()
    body = re.search(r"const body=\{([^}]*)\}", html)
    assert body, "could not find the save body in setup_wizard.html"
    assert "connectors" not in body.group(1), (
        "the wizard sends a `connectors` key again. It no longer has a checkbox "
        "list to build it from, so whatever it sends is not the user's choice — "
        "and an empty list overwrites the default rather than leaving it.")


def test_the_connectors_api_survives_for_the_hub() -> None:
    """The step went, the capability did not. hubApi.setupConnectors() and the
    QA contract tests both read this route."""
    assert '@router.get("/api/setup/connectors")' in WIZARD_PY.read_text(encoding="utf-8"), (
        "/api/setup/connectors was removed along with the wizard step, but "
        "Setup -> Connectors in the SPA still calls it")
