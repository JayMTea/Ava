"""A hub panel may not reach a sibling with a single-dot relative import.

ava_bridge/hub_api.py is being split into ava_bridge/hub/<panel>.py. Handlers
moved one package deeper, so every `from . import x` in them silently changed
meaning: inside hub_api.py `.` was `ava_bridge`, inside a panel it is
`ava_bridge.hub`.

The first extraction hit the worst version of this. `models_list` did
`from . import models as model_store` to reach ava_bridge/models.py — and the
panel it moved into is itself named models.py, so the import kept working and
resolved to the module doing the importing. No ImportError; instead
`AttributeError: module 'ava_bridge.hub.models' has no attribute 'manifest'`
from inside a request, which qa caught only because it exercises the real app.

A panel legitimately needs the package above it (`from .. import settings`).
What it must not do is single-dot, which either shadows itself or picks up a
sibling panel. Panels are siblings by accident of layout, not by design; they
share code through ava_bridge/, not through each other.
"""
import ast
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
HUB = ROOT / "ava_bridge" / "hub"


def _tracked_panels() -> list[pathlib.Path]:
    if not HUB.is_dir():
        return []
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "ava_bridge/hub/*.py"],
                         capture_output=True, text=True, check=True).stdout
    return [ROOT / p for p in out.splitlines() if p]


def test_panels_never_use_a_single_dot_relative_import() -> None:
    offenders = []
    for path in _tracked_panels():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                names = ", ".join(a.name for a in node.names)
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: "
                    f"from . import {names}")
    assert not offenders, (
        "a hub panel used a single-dot relative import:\n  "
        + "\n  ".join(offenders)
        + "\n\nInside ava_bridge/hub/, `.` is the hub package, not ava_bridge. "
        "This either resolves to the panel itself (if the names collide, which "
        "is how the models panel broke) or reaches a sibling panel. Use `..` to "
        "reach ava_bridge, and share code through ava_bridge/ rather than "
        "between panels."
    )
