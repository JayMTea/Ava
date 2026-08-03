"""Every icon name declared anywhere must exist in the one icon set.

`frontend/src/lib/icons.tsx` `ICONS` is the whole vocabulary. Three places name
an icon by string and none of them is checked against it:

  * `HubView.tsx` `TABS[].icon`   — Setup's six tabs
  * `Drawer.tsx`  `NAV[].icon`    — the sidebar destinations
  * `connector.yaml` `ui.icon`    — a connected app's chosen glyph

An unknown name is not an error anywhere. `appIcon()` returns null for an
undeclared icon ON PURPOSE, so it can hash the app id into a stable glyph
(CLAUDE.md, "App icons follow the same rule") — which is right for an app that
declared nothing, and wrong for one that declared a typo: the manifest asked for
a glyph and silently got a different one. `hub/connectors.py` validates `ui.icon`
against `_ICON_RE`, which checks the SHAPE of the string and never the set.
A mistyped tab or nav icon simply renders nothing.

Note the connector half is a ratchet, not a current finding: no tracked manifest
declares `ui.icon` today, so it guards the next one rather than an existing bug.
The tab and nav halves are live.

Style matches tests/test_diagram_sync.py: a static scan over `git ls-files` —
no npm, no build, no bridge.
"""
import re

import yaml

from gitfiles import ROOT, require_git, tracked

ICONS_TSX = "frontend/src/lib/icons.tsx"
HUBVIEW = "frontend/src/components/hub/HubView.tsx"
DRAWER = "frontend/src/components/Drawer.tsx"


def _read(rel: str) -> str:
    require_git()
    assert rel in tracked(rel), f"{rel} is not tracked — did it move?"
    return (ROOT / rel).read_text(encoding="utf-8")


def _icon_names() -> set[str]:
    """The keys of the ICONS record — the entire available vocabulary."""
    m = re.search(r"export const ICONS[^=]*=\s*\{(.*?)\n\};", _read(ICONS_TSX), re.S)
    assert m, (
        f"could not find the ICONS record in {ICONS_TSX}. This guard reads the "
        "vocabulary out of the source, so a reformat would otherwise reduce it to "
        "an empty set and pass every check trivially."
    )
    found = set(re.findall(r"^\s{2}([A-Za-z_][\w-]*):", m.group(1), re.M))
    assert found, f"the ICONS record in {ICONS_TSX} parsed to zero keys"
    return found


def _declared(rel: str, const: str) -> dict[str, str]:
    """{id: icon} for a `const NAME = [ { id: '…', icon: '…' } ]` declaration.

    The closing bracket may be indented: HubView's TABS is module-level, Drawer's
    NAV is declared inside the component.
    """
    m = re.search(rf"const {const}\b[^=]*=\s*\[(.*?)\n\s*\];", _read(rel), re.S)
    assert m, f"could not find `const {const}` in {rel}"
    found = dict(re.findall(r"\{\s*id:\s*'([^']+)'[^}]*?icon:\s*'([^']+)'", m.group(1)))
    assert found, f"`const {const}` in {rel} parsed to zero entries"
    return found


def _manifest_icons() -> list[tuple[str, str, str]]:
    """(path, connector id, icon) for every tracked manifest declaring ui.icon."""
    paths = tracked("*connector.yaml")
    assert paths, "no tracked connector.yaml found — did the connectors move?"
    out: list[tuple[str, str, str]] = []
    for rel in paths:
        try:
            doc = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue  # tests/test_connectors.py owns manifest validity
        icon = (doc.get("ui") or {}).get("icon")
        if isinstance(icon, str) and icon:
            out.append((rel, str(doc.get("id") or rel), icon))
    return out


# ── the invariants ──────────────────────────────────────────────────────────

def test_setup_tab_icons_exist() -> None:
    known = _icon_names()
    bad = {tab: icon for tab, icon in _declared(HUBVIEW, "TABS").items() if icon not in known}
    assert not bad, (
        f"Setup tab(s) name an icon that {ICONS_TSX} does not define: {bad}\n"
        "The tab strip renders nothing for an unknown name — no error, no "
        "fallback glyph, just a missing icon beside a live tab. Add it to ICONS "
        "or use one of the existing names."
    )


def test_sidebar_nav_icons_exist() -> None:
    known = _icon_names()
    bad = {nav: icon for nav, icon in _declared(DRAWER, "NAV").items() if icon not in known}
    assert not bad, (
        f"sidebar destination(s) name an icon that {ICONS_TSX} does not define: {bad}\n"
        "Same failure as a Setup tab: the rail renders an empty slot rather than "
        "complaining. Add it to ICONS or use an existing name."
    )


def test_connector_manifest_icons_exist() -> None:
    known = _icon_names()
    bad = [(rel, cid, icon) for rel, cid, icon in _manifest_icons() if icon not in known]
    assert not bad, (
        "connector manifest(s) declare a ui.icon that "
        f"{ICONS_TSX} does not define:\n  "
        + "\n  ".join(f"{cid} -> {icon!r}  ({rel})" for rel, cid, icon in bad)
        + "\nappIcon() returns null for an undeclared icon on purpose, so it can "
        "hash the app id into a stable glyph — which means this connector "
        "silently loses the glyph its manifest asked for, with no error anywhere. "
        "Either add it to ICONS, or drop ui.icon and let the hash choose."
    )
