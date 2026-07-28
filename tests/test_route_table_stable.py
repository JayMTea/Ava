"""The route table must not change shape while routes are only being MOVED.

This exists for the phone_bridge.py split (86 route decorators across one
2,144-line module, being extracted into per-concern APIRouters). The refactor's
contract is "same routes, different files" — so the whole risk is a route that
silently changes path, loses a method, or disappears.

Why a snapshot and not a count. `qa/test_01_auth_surface.py` is the real safety
net for auth posture, but it GENERATES its checks from `app.routes`: a route
that vanishes stops being enumerated, so it stops being checked rather than
failing. The absence is invisible to it. A frozen (path, methods, kind) set has
the opposite property — anything that disappears, moves, or gains a method is a
diff.

Why the exact set and not just `len()`. A count catches deletion but not a
swap: drop `/api/chats/{cid}` and add `/api/chat/{cid}` in the same commit and
the number is unchanged. Paths are also what the /internal scope gate keys on
(auth.auth_gate -> internal.group_may(group, path)), so a path typo during an
extraction is a security-relevant change, not a cosmetic one.

Updating this file is meant to be deliberate. When you INTEND to add or remove a
route, regenerate the snapshot in the same commit that changes the routes, so
the diff shows the route change next to the code change:

    python -c "import json; \
from tests.test_route_table_stable import _live_routes; \
open('tests/_route_table.json','w').write(json.dumps(_live_routes(),indent=1)+'\n')"

Never regenerate it to make a red test go green without reading the diff first —
that is the one action this guard exists to prevent.
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "_route_table.json"


def _live_routes() -> list[list]:
    """The app's route table, EXCLUDING the optional private overlay.

    overlay/ava_bridge/personal_routes.py registers the owner's own apps when
    that tree is present, and it is gitignored — a fork has none. Snapshotting
    them was wrong twice over: the fixture failed on every machine without the
    overlay (CI included), and it wrote the owner's private route paths into a
    TRACKED file, which is exactly what tests/test_no_owner_identity.py exists
    to stop. Filter on the endpoint's defining module so the snapshot describes
    only what ships.
    """
    import phone_bridge

    rows = []
    for r in phone_bridge.app.routes:
        path = getattr(r, "path", None)
        if path is None:
            continue
        endpoint = getattr(r, "endpoint", None)
        module = getattr(endpoint, "__module__", "") or ""
        if module.startswith("overlay"):
            continue
        rows.append([path, sorted(getattr(r, "methods", None) or []), type(r).__name__])
    rows.sort()
    return rows


def _key(rows):
    return {(p, tuple(m)) for p, m, _kind in rows}


def test_route_table_matches_the_snapshot() -> None:
    expected = json.loads(SNAPSHOT.read_text())
    live = _live_routes()

    exp_k, live_k = _key(expected), _key(live)
    missing = sorted(exp_k - live_k)
    added = sorted(live_k - exp_k)

    detail = []
    if missing:
        detail.append("GONE or CHANGED (in snapshot, not in the app):\n    "
                      + "\n    ".join(f"{p}  {list(m)}" for p, m in missing))
    if added:
        detail.append("NEW or CHANGED (in the app, not in the snapshot):\n    "
                      + "\n    ".join(f"{p}  {list(m)}" for p, m in added))

    assert not detail, (
        "the route table changed.\n\n" + "\n\n".join(detail)
        + "\n\nIf you were MOVING routes into a router, this is a real defect: a "
        "path or method changed in the move. Fix the router registration.\n"
        "If you INTENDED to add or remove a route, regenerate tests/_route_table.json "
        "in this same commit (see this file's docstring) so the route change is "
        "visible in the diff."
    )


def test_snapshot_has_no_duplicate_path_and_method_pairs() -> None:
    """Two handlers on one (path, method) means the second is dead.

    FastAPI matches in registration order and does not warn. During a split this
    is the likely shape of a mistake: a route left behind in phone_bridge.py AND
    added to the new router, where whichever registers first wins silently.
    """
    live = _live_routes()
    seen, dupes = set(), []
    for path, methods, _kind in live:
        for m in methods or ["<mount>"]:
            if (path, m) in seen:
                dupes.append(f"{m} {path}")
            seen.add((path, m))
    assert not dupes, (
        "these (path, method) pairs are registered twice, so the later handler is "
        "unreachable — FastAPI matches in registration order and says nothing:\n  "
        + "\n  ".join(dupes)
        + "\n\nDuring an extraction this usually means the route was added to the "
        "new router but not deleted from phone_bridge.py."
    )
