"""Four files have to agree about route memory, or an app quietly stops being
reopened where it was left.

The reporter now lives in THREE places at once: the script the bridge injects
(`ava_bridge/assets/app_route.js`), the validator that accepts what it sends
(`frontend/src/lib/embedNavigation.ts`), and the manual an app author follows to
do it themselves (`docs/CONNECTOR_SDK.md` §3). They are in three languages,
nothing imports anything, and every disagreement between them fails silently:
a parameter the shim forgets to strip is a token in the shell's address bar, a
mode the manifest accepts but nobody documents is a field owners cannot find,
and a `.ava` path the shell stopped refusing is a reserved namespace an app
could be reopened inside.

Style is tests/test_diagram_sync.py: a static scan over `git ls-files`, no
bridge, no AVA_HOME, no network, no imports of the code under test — so it runs
in CI, in the built image, and on a box that cannot import the bridge at all.
"""
from __future__ import annotations

import pathlib
import re

from gitfiles import require_git

ROOT = pathlib.Path(__file__).resolve().parents[1]

SHIM = ROOT / "ava_bridge" / "assets" / "app_route.js"
VALIDATOR = ROOT / "frontend" / "src" / "lib" / "embedNavigation.ts"
DOCS = ROOT / "docs" / "CONNECTOR_SDK.md"
CONNECTORS = ROOT / "ava_bridge" / "connectors.py"
EMBED_ROUTE = ROOT / "ava_bridge" / "embed_route.py"
BRIDGE = ROOT / "phone_bridge.py"


def _read(path: pathlib.Path) -> str:
    require_git()
    assert path.exists(), f"{path.relative_to(ROOT)} has moved — update this guard"
    return path.read_text(encoding="utf-8")


def test_the_shim_strips_exactly_what_the_shell_strips() -> None:
    """The launch parameters the frame's `src` carries.

    `t` is an embed token. A remembered address that carries one is a URL that
    cannot be reopened and a credential in the browser's history for nothing.
    The shell strips them again on arrival, so a drift here is invisible until
    somebody reads the address bar.
    """
    shim = re.search(r"var DROP = /\^\(([^)]+)\)\$/", _read(SHIM))
    validator = re.search(r"/\^\(([^)]+)\)\$\|", _read(VALIDATOR))
    assert shim and validator, (
        "the launch-parameter list is no longer written as `^(a|b)$` in "
        f"{SHIM.name} or {VALIDATOR.name} — update this guard with it")
    assert shim.group(1) == validator.group(1), (
        f"the injected reporter strips ({shim.group(1)}) while the shell "
        f"strips ({validator.group(1)})")
    documented = _read(DOCS)
    for name in shim.group(1).split("|"):
        assert f"`{name}`" in documented, (
            f"the reporter strips `{name}` and docs/CONNECTOR_SDK.md §3 does "
            "not say so")


def test_neither_reporter_re_serialises_the_query_it_keeps() -> None:
    """The pairs that survive have to survive BYTE FOR BYTE, on both sides.

    `URLSearchParams.toString()` re-encodes every survivor — `%20` becomes `+`,
    `:` and `,` become `%3A`/`%2C`, a bare flag gains `=`. The string the shim
    posts is the string the shell stores and the address the frame is reopened
    at, so either half reaching for it hands the app back a URL its own router
    never produced. Both filter the raw `&`-separated pairs instead, decoding
    only the key to judge it.
    """
    for path in (SHIM, VALIDATOR):
        # Comments stripped: both files DISCUSS `URLSearchParams` at length,
        # which is the point. The check is on what the code reaches for.
        src = re.sub(r"/\*.*?\*/", "", _read(path), flags=re.S)
        src = "\n".join(line.split("//")[0] for line in src.splitlines())
        assert "URLSearchParams" not in src, (
            f"{path.name} re-serialises the query it reports; filter the raw "
            "pairs and keep the bytes the app wrote")
        assert "split('&')" in src or 'split("&")' in src, (
            f"{path.name} no longer filters the query pair by pair — update "
            "this guard with whatever replaced it")


def test_the_reserved_namespace_is_reserved_on_both_sides() -> None:
    """`/.ava/` on an app's mount is Ava's, never the app's.

    The external form of the shim is served from it, and the shell refuses to
    remember a path under it. Lose the refusal and an app can be reopened at a
    bridge route it does not serve.
    """
    assert re.search(r"\\\.ava\)\(\\/\|\$\)", _read(VALIDATOR)), (
        "embedNavigation.ts no longer refuses to remember a `/.ava` path, so "
        "the bridge's own routes on an app's mount are reachable as a "
        "remembered destination")
    assert '"/apps/{cid}/.ava/route.js"' in _read(BRIDGE), (
        "the route that serves the external reporter has moved; "
        "embed_route.script_url must move with it")
    assert 'f"/apps/{cid}/.ava/route.js"' in _read(EMBED_ROUTE), (
        "embed_route.script_url no longer points at the route phone_bridge "
        "registers — a CSP-restricted app would load a 404 as its reporter")


def test_every_route_mode_the_manifest_takes_is_documented() -> None:
    """`ui.route` is a manifest field with no Setup switch, so the SDK page is
    the only place an owner or an app author can learn it exists."""
    declared = re.search(r"^ROUTE_MODES = \(([^)]+)\)", _read(CONNECTORS),
                         re.MULTILINE)
    assert declared, "ROUTE_MODES is no longer a literal tuple in connectors.py"
    modes = [m.strip().strip("\"'") for m in declared.group(1).split(",")
             if m.strip()]
    heading = "`ui.route: " + " | ".join(modes) + "`"
    docs = _read(DOCS)
    assert heading in docs, (
        f"docs/CONNECTOR_SDK.md §3 does not head the switch {heading} — "
        "a mode added to the manifest has to be added to the manual")
    assert "  route: " in docs, (
        "`ui.route` is missing from the manifest field reference in "
        "docs/CONNECTOR_SDK.md §2")


def test_the_message_the_shim_posts_is_the_message_the_shell_accepts() -> None:
    shim, validator, docs = _read(SHIM), _read(VALIDATOR), _read(DOCS)
    for token in ("ava:navigation", "cid", "path"):
        assert token in shim and token in validator, token
    assert "'ava:theme-request'" in shim and "ava:theme-request" in docs, (
        "the origin handshake is documented as an app's fallback and used by "
        "the injected reporter; both have to name the same message")
    assert '{type: "ava:navigation", cid, path}' in docs
