"""Every API path the SPA calls must exist on the bridge.

The frontend and the bridge are one repo but two artifacts, and nothing tied
them together: `frontend/src` could name `/api/hub/models/store` while
`ava_bridge` had no such route, and the only place that mismatch showed up was
an owner's browser, as `Couldn't load the model store. /api/hub/models/store ->
404`. A raw path and a status code, on the newest panel, is the least
diagnosable failure this app can produce — it reads as "the model store is
broken" when the truth is "that route is not there".

This is a static scan (no browser, no build) over the tracked frontend sources:
pull every `/api/...` string literal out and require that the live route table
has a route it could match. Template segments (`${encodeURIComponent(id)}`)
collapse to a wildcard and match a `{param}` segment — the check is on the
SHAPE of the path, which is what routing matches on.

It does NOT prove an install is consistent: a bridge that is running older code
than the page it served still 404s, and no test in this repo can see that. That
failure is named at runtime instead — `lib/api.req` marks a bodyless 404 as
`bridge_outdated` and the Hub renders "Restart Ava to load …". This guard
covers the half that IS knowable at commit time: the repo never ships a page
that calls a route the repo does not have.

Mirrors the tests/test_diagram_sync.py convention-guard style.
"""
import pathlib
import re

from gitfiles import tracked_paths as _tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

# String literals (single, double or backtick) that start a bridge path. `/apps`
# is deliberately absent: those are reverse proxies to a connector's own server,
# whose routes are not ours to know.
_LITERAL = re.compile(r"""['"`](/(?:api|internal)/[^'"`\s]*)['"`]""")

# `${…}` is one path segment's worth of value at runtime. Nested braces do not
# occur in these call sites (`${encodeURIComponent(id)}` is the deepest form),
# so a non-greedy match to the first `}` is right.
_TEMPLATE = re.compile(r"\$\{[^{}]*\}")

# Not calls. Each is a literal that LOOKS like a path but is never fetched, so
# there is nothing for the route table to answer — with the reason, not a bare
# allowlist.
_NOT_A_CALL = {
    # ConnectApp's action-path input placeholder: an example of a path on the
    # OWNER'S app, typed by them, served by their server.
    "/api/notes",
}


def _spa_paths() -> dict[str, set[str]]:
    """Every bridge path literal in the tracked frontend sources -> where."""
    found: dict[str, set[str]] = {}
    for f in _tracked("frontend/src/**/*.ts") + _tracked("frontend/src/**/*.tsx"):
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(ROOT).as_posix()
        for m in _LITERAL.finditer(text):
            raw = m.group(1)
            # A glob in a comment ("cookie-gated /api/hub/*") is documentation.
            if "*" in raw or raw in _NOT_A_CALL:
                continue
            path = raw.split("?")[0].rstrip("/") or "/"
            # A bare namespace ('/api/') is a prefix test — `req` uses one to
            # decide whether a 404 is Ava's own — not an endpoint to resolve.
            if path in ("/api", "/internal"):
                continue
            found.setdefault(path, set()).add(rel)
    return found


def _live_paths() -> list[list[str]]:
    """The bridge's routes, pre-split into segments for shape matching."""
    import phone_bridge

    out = []
    for r in phone_bridge.app.routes:
        path = getattr(r, "path", None)
        if path:
            out.append(path.strip("/").split("/"))
    return out


def _matches(path: str, routes: list[list[str]]) -> bool:
    segs = _TEMPLATE.sub("\x00", path).strip("/").split("/")
    for route in routes:
        if len(route) != len(segs):
            continue
        # A `{param}` route segment eats anything; a `${…}` call segment can be
        # anything, so it satisfies a literal route segment too.
        if all(r.startswith("{") or s == "\x00" or s == r
               for s, r in zip(segs, route)):
            return True
    return False


def test_every_api_path_the_spa_calls_exists_on_the_bridge() -> None:
    routes = _live_paths()
    assert routes, "no routes found on phone_bridge.app"

    offenders = []
    for path, files in sorted(_spa_paths().items()):
        if not _matches(path, routes):
            offenders.append(f"{path}  <- {', '.join(sorted(files))}")
    assert not offenders, (
        "the SPA calls path(s) no bridge route can answer, so the owner gets a "
        "404 on whichever panel asks for them:\n  "
        + "\n  ".join(offenders)
        + "\n\nAdd the route (ava_bridge/…) and regenerate "
        "tests/_route_table.json, or fix the path in the frontend. If the "
        "literal is not a call at all, add it to _NOT_A_CALL with the reason."
    )
