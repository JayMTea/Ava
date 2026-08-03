"""Every example connector must be one a forker can copy and have work.

There used to be a companion, `tests/test_example_app.py`, which exercised one
example deeply - it booted the server and spoke the protocol - but it was
hardcoded to that single folder, so every example added after it shipped with no
validation at all. That is the wrong way round: the examples are the first code a
forker copies, and a broken one is worse than a missing one because it looks
authoritative. When that example was retired the deep test went with it, and this
file is what remains: the half that covers whatever is actually in `examples/`.

These are cheap, static checks over whatever is in `examples/`, so a new example is
covered the moment it lands rather than when someone remembers to widen a test.
Each assertion message is the fix instruction, per tests/test_diagram_sync.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from ava_bridge import connectors

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted(p for p in (ROOT / "examples").glob("*/connector.yaml"))
# The curated glyph set lives in TypeScript, so this is a cross-language read of the
# same kind tests/test_platform_matrix_ssot.py already does. Parsed rather than
# restated, because a copy of the list here would be one more thing to drift.
APP_ICONS_TS = ROOT / "frontend" / "src" / "lib" / "appColor.tsx"


def _manifest(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _app_icons() -> set[str]:
    src = APP_ICONS_TS.read_text(encoding="utf-8")
    m = re.search(r"export const APP_ICONS\s*=\s*\[(.*?)\]", src, re.S)
    assert m, (f"could not find APP_ICONS in {APP_ICONS_TS.relative_to(ROOT)} — if it "
               "moved, update this test rather than deleting the icon check, or a "
               "manifest can name a glyph that silently does not resolve.")
    return set(re.findall(r"'([a-z0-9-]+)'", m.group(1)))


def test_there_are_examples_to_check() -> None:
    """A floor, so this file cannot pass vacuously if examples/ moves."""
    assert EXAMPLES, ("no examples/*/connector.yaml found — did the examples move? "
                      "Every assertion below would pass trivially.")


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.parent.name)
def test_the_manifest_passes_the_loader(path: Path) -> None:
    """The example a forker copies must never be what trips the validator."""
    errors: list = []
    connectors._validate(_manifest(path), str(path), errors)
    hard = [e for e in errors if e.get("severity") != "warn"]
    assert not hard, f"{path.relative_to(ROOT)} is invalid: {hard}"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.parent.name)
def test_a_declared_icon_actually_resolves(path: Path) -> None:
    """`ui.icon` outside APP_ICONS is not an error — it is worse.

    `appIcon()` falls back to a glyph hashed from the id, so a typo does not fail
    anything: the app just quietly gets a different icon than the author chose, and
    the Hub's appearance picker cannot offer it either.
    """
    icon = ((_manifest(path).get("ui") or {}).get("icon") or "").strip()
    if not icon:
        return                      # omitting it is legitimate — auto-pick handles it
    icons = _app_icons()
    assert icon in icons, (
        f"{path.relative_to(ROOT)} declares ui.icon '{icon}', which is not in "
        f"APP_ICONS ({sorted(icons)}). appIcon() would silently substitute a hashed "
        "glyph. Pick one from that list, or add the glyph to APP_ICONS and lib/icons.")


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.parent.name)
def test_every_declared_tier_is_a_real_tier(path: Path) -> None:
    """A misspelled tier is a security defect, not a cosmetic one.

    `_dynamic_access` falls back to `write` (or `physical` on a device) for anything
    it cannot match, so `sensitve:` does not raise — it downgrades the tool to the
    default and the author believes they classified it.
    """
    da = _manifest(path).get("dynamic_access")
    if not isinstance(da, dict):
        return
    for pattern, tier in da.items():
        assert str(tier).lower() in connectors._TIERS, (
            f"{path.relative_to(ROOT)} maps '{pattern}' to '{tier}', which is not a "
            f"consent tier ({', '.join(connectors._TIERS)}).")


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.parent.name)
def test_an_iframe_example_ships_the_server_it_tells_you_to_run(path: Path) -> None:
    """`embed: iframe` means the app serves its own UI, so the example has to
    include that server — otherwise its README instructs a forker to run a file
    that is not there, and the app view renders an error."""
    ui = _manifest(path).get("ui") or {}
    if str(ui.get("embed") or "") != "iframe":
        return
    assert (path.parent / "server.py").exists(), (
        f"{path.parent.relative_to(ROOT)} declares ui.embed: iframe but ships no "
        "server.py. Either add one or use embed: none, which renders Ava's generic "
        "action console instead of proxying a UI that does not exist.")


def test_no_two_examples_collide_on_id_or_port() -> None:
    """They are meant to be installable side by side — the screenshots run all of
    them at once. Two examples sharing a port means the second one silently fails
    to bind and its tile shows down."""
    ids: dict[str, str] = {}
    ports: dict[int, str] = {}
    for path in EXAMPLES:
        m = _manifest(path)
        name = path.parent.name
        cid = str(m.get("id") or "")
        assert cid, f"{path.relative_to(ROOT)} declares no id"
        assert cid not in ids, (
            f"{name} and {ids[cid]} both use id '{cid}'. Ava keys connectors by id, "
            "so installing both would shadow one.")
        ids[cid] = name
        for url in (str((m.get("ui") or {}).get("url") or ""),
                    str((m.get("service") or {}).get("probe") or "")):
            hit = re.search(r"127\.0\.0\.1:(\d+)", url)
            if not hit:
                continue
            port = int(hit.group(1))
            owner = ports.setdefault(port, name)
            assert owner == name, (
                f"{name} and {owner} both use port {port}. Give each example its own, "
                "so a forker can run them together.")
