"""Convention guard: the installer may open the first-run link, never depend on it.

Copying a claim token out of a terminal is ceremony, not security — the token is
a proof you own the machine, not a secret the owner is meant to custody — so
install.sh opens the link for a local desktop session the way `jupyter notebook`
does. That convenience is only safe while two properties hold, and both are the
kind that erode silently:

  1. The link is PRINTED unconditionally, before any attempt to open it. If
     opening ever became the primary path, every headless and SSH install would
     end with a working instance and no way in.
  2. Opening is SKIPPED where it would be wrong rather than merely useless —
     over SSH it targets the wrong machine, in CI there is no session and
     xdg-open can block on a dbus call that never answers, and on headless Linux
     it prints an error that reads like an install failure.

Style follows tests/test_install_detection.py — a static scan that runs anywhere,
with no shell and no Docker.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "install.sh"


def _src() -> str:
    return INSTALL.read_text(encoding="utf-8")


def test_the_link_is_printed_before_any_attempt_to_open_it() -> None:
    src = _src()
    printed = src.find('say "  $_link"')
    opened = src.find('open_browser "$_link"')
    assert printed != -1, (
        "install.sh no longer prints the claim link on its own line. Terminals "
        "wrap long URLs and a link copied in two pieces loses the query string, "
        "which on this URL is the only part that matters.")
    assert opened != -1, "install.sh no longer offers to open the claim link"
    assert printed < opened, (
        "install.sh opens the browser before printing the link. If the open "
        "fails — headless, SSH, no xdg-open — the owner is left with a running "
        "instance and no way to claim it.")


def test_every_wrong_place_to_open_a_browser_is_refused() -> None:
    src = _src()
    for guard, why in (
        ("AVA_NO_BROWSER", "no opt-out, so an owner who does not want this cannot say so"),
        ("${CI:-}", "CI has no session to open into and xdg-open can block there"),
        ("SSH_CONNECTION", "over SSH the browser opens on the wrong machine"),
        ("WAYLAND_DISPLAY", "headless Linux has no display to open into"),
    ):
        assert guard in src, f"install.sh dropped the {guard} check — {why}"


def test_opening_can_never_fail_the_install() -> None:
    """A browser that will not open is not an install failure. install.sh runs
    under `set -euo pipefail`, so every branch must end in an explicit success."""
    src = _src()
    start = src.find("open_browser() {")
    assert start != -1, "open_browser() is gone"
    body = src[start:src.find("\n}", start)]
    assert body.count("return 0") >= 5, (
        "open_browser() has a path that does not end in `return 0`. Under "
        "set -e a non-zero return from it aborts the installer after the stack "
        "is already up, which reports failure for a working install.")
    assert "|| true" not in body.split("case", 1)[0], (
        "the guards were softened with `|| true`, which hides a real refusal")
