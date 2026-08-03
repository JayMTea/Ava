"""Setup's tab list, its TabId union, and its retired addresses must agree.

Three hand-maintained lists describe the same six Setup tabs and nothing checked
them against each other:

  * `frontend/src/components/hub/HubView.tsx`  renders `TABS`
  * `frontend/src/components/hub/shared.ts`    declares the `TabId` union that
    `hubRoute.ts` validates every address against
  * `frontend/src/components/hub/hubRoute.ts`  keeps retired addresses alive in
    MOVED_TABS / MERGED_TABS / RELOCATED_TABS

When TABS and TabId diverge, a tab either renders but cannot be addressed, or is
addressable and renders nothing. And `shared.ts` already states the redirect
invariant in a comment — "a redirect keyed on a live tab would never fire" —
with no enforcement behind it, which is exactly the shape of rule that rots.

Also covered here, because it is the same class of silent breakage:
`ava_bridge/hub_api.py` imports one panel module per Setup surface and then
includes each one's router. An imported-but-unincluded panel is a whole tab's API
absent at runtime, and the import makes it look wired.

Style matches tests/test_diagram_sync.py: a static scan over `git ls-files` — no
bridge, no browser, no build, no npm. Every parser below asserts it matched
something, so a refactor that moves a declaration fails this guard loudly instead
of silently reducing it to an empty set and passing.
"""
import ast
import re

from gitfiles import ROOT, require_git, tracked

HUBVIEW = "frontend/src/components/hub/HubView.tsx"
SHARED = "frontend/src/components/hub/shared.ts"
HUBROUTE = "frontend/src/components/hub/hubRoute.ts"
APP = "frontend/src/App.tsx"
HUB_API = "ava_bridge/hub_api.py"


def _read(rel: str) -> str:
    require_git()
    assert rel in tracked(rel), f"{rel} is not tracked — did it move?"
    return (ROOT / rel).read_text(encoding="utf-8")


def _block(text: str, pattern: str, what: str) -> str:
    """The source of a declaration, or a failure naming what went missing."""
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find {what}. This guard reads it out of the source, so a "
        f"declaration that moved or was reformatted disables the check — which "
        f"is why this is a failure and not an empty result. Update the pattern "
        f"in tests/test_hub_tab_ssot.py alongside the move."
    )
    return m.group(1)


def _tabs() -> list[tuple[str, str]]:
    """(id, icon) for every Setup tab, in render order."""
    body = _block(_read(HUBVIEW), r"const TABS:[^=]*=\s*\[(.*?)\n\];", "TABS in " + HUBVIEW)
    found = re.findall(r"\{\s*id:\s*'([^']+)'[^}]*?icon:\s*'([^']+)'", body)
    assert found, f"TABS in {HUBVIEW} parsed to zero entries"
    return found


def _tab_ids() -> set[str]:
    return {t for t, _ in _tabs()}


def _tab_id_union() -> set[str]:
    body = _block(_read(SHARED), r"export type TabId\s*=(.*?);", "the TabId union in " + SHARED)
    found = set(re.findall(r"'([^']+)'", body))
    assert found, f"the TabId union in {SHARED} parsed to zero members"
    return found


def _agent_subtabs() -> set[str]:
    body = _block(_read(HUBROUTE), r"export const AGENT_SUBTABS\s*=\s*\[(.*?)\n\]", "AGENT_SUBTABS in " + HUBROUTE)
    found = set(re.findall(r"id:\s*'([^']+)'", body))
    assert found, f"AGENT_SUBTABS in {HUBROUTE} parsed to zero entries"
    return found


def _redirect_table(name: str) -> dict[str, str]:
    body = _block(_read(HUBROUTE), rf"export const {name}[^=]*=\s*\{{(.*?)\n\}};", f"{name} in {HUBROUTE}")
    found = dict(re.findall(r"(\w+):\s*'([^']+)'", body))
    assert found, f"{name} in {HUBROUTE} parsed to zero entries"
    return found


def _builtin_views() -> set[str]:
    body = _block(_read(APP), r"const BUILTIN_VIEWS\s*=\s*\[(.*?)\]", "BUILTIN_VIEWS in " + APP)
    found = set(re.findall(r"'([^']+)'", body))
    assert found, f"BUILTIN_VIEWS in {APP} parsed to zero entries"
    return found


def _hub_api_panels() -> tuple[set[str], set[str]]:
    """(imported, included) panel module names, read with ast rather than regex.

    ast because the import is a wrapped multi-line parenthesised form with a
    `# noqa` inside it — exactly the shape a regex gets subtly wrong.
    """
    tree = ast.parse(_read(HUB_API))
    imported: set[str] = set()
    included: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "hub":
            imported |= {a.name for a in node.names}
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
            for elt in node.iter.elts:
                if isinstance(elt, ast.Name) and elt.id.startswith("_hub_"):
                    included.add(elt.id[len("_hub_"):])
    assert imported, f"no `from .hub import (...)` found in {HUB_API}"
    assert included, f"no panel-include loop found in {HUB_API}"
    return imported, included


# ── the invariants ──────────────────────────────────────────────────────────

def test_hubview_tabs_match_the_tabid_union() -> None:
    tabs, union = _tab_ids(), _tab_id_union()
    only_tabs, only_union = sorted(tabs - union), sorted(union - tabs)
    assert not (only_tabs or only_union), (
        f"Setup's tab list and its TabId union disagree:\n"
        f"  only in TABS ({HUBVIEW}): {only_tabs}\n"
        f"  only in TabId ({SHARED}): {only_union}\n"
        "HubView renders TABS; hubRoute.ts validates every address against TabId. "
        "When they diverge a tab either renders and cannot be addressed, or is "
        "addressable and renders nothing. Both files are in "
        "frontend/src/components/hub/ — change them together."
    )


def test_no_retired_address_collides_with_a_live_tab() -> None:
    union = _tab_id_union()
    retired = {**_redirect_table("MOVED_TABS"),
               **_redirect_table("MERGED_TABS"),
               **_redirect_table("RELOCATED_TABS")}
    collisions = sorted(set(retired) & union)
    assert not collisions, (
        f"these retired addresses are also live Setup tabs: {collisions}\n"
        f"{SHARED} says it plainly: \"a redirect keyed on a live tab would never "
        "fire\". parseHubHash resolves a live tab before it consults the redirect "
        "tables, so the entry is dead code that reads like a working redirect. "
        "Either drop the redirect or rename the live tab."
    )


def test_every_retired_address_points_somewhere_real() -> None:
    union, subs, views = _tab_id_union(), _agent_subtabs(), _builtin_views()
    bad: list[str] = []
    for key, dest in _redirect_table("MERGED_TABS").items():
        if dest not in union:
            bad.append(f"MERGED_TABS[{key!r}] -> {dest!r}, which is not a TabId")
    for key, dest in _redirect_table("MOVED_TABS").items():
        if dest not in subs:
            bad.append(f"MOVED_TABS[{key!r}] -> {dest!r}, which is not an AGENT_SUBTABS id")
    for key, dest in _redirect_table("RELOCATED_TABS").items():
        if dest.split("/")[0] not in views:
            bad.append(f"RELOCATED_TABS[{key!r}] -> {dest!r}, whose view segment is not in BUILTIN_VIEWS")
    assert not bad, (
        "these retired Setup addresses redirect to somewhere that no longer exists:\n  "
        + "\n  ".join(bad)
        + "\nA redirect whose target moved lands the visitor on Overview through the "
        "unknown-segment fallback, which is indistinguishable from \"that feature was "
        "removed\" — the exact outcome hubRoute.ts's redirect tables exist to prevent. "
        "Retarget the entry, or retire it."
    )


def test_every_imported_hub_panel_is_included() -> None:
    imported, included = _hub_api_panels()
    missing = sorted(imported - included)
    assert not missing, (
        f"{HUB_API} imports panel module(s) it never includes: {missing}\n"
        "An imported-but-unincluded panel is a whole Setup tab's API silently "
        "absent at runtime, and the import is what makes it look wired. Add it to "
        "the `for _panel in (...)` tuple, or drop the import."
    )


def test_every_hub_panel_module_is_mounted() -> None:
    imported, _ = _hub_api_panels()
    on_disk = {p.rsplit("/", 1)[-1][:-3] for p in tracked("ava_bridge/hub/*.py")}
    on_disk.discard("__init__")
    orphans = sorted(on_disk - imported)
    assert not orphans, (
        f"panel module(s) in ava_bridge/hub/ that {HUB_API} never mounts: {orphans}\n"
        "Every panel exposes a prefix-less router that only becomes reachable when "
        "hub_api includes it, so an unmounted module is a route nothing can call. "
        "Mount it, or delete it."
    )
