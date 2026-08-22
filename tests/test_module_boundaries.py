"""A name is private or it is shared — it cannot be both.

`ava_bridge` modules imported each other's underscore-prefixed names: turns.py
took `_chat_append` from chat_store, artifacts.py and turns.py both took
`_sbx_read` and `_session_file` from agent, phone_bridge took `_augment`,
`_parse_ids`, `_safe_name` from documents. Eight names in all, one of them with
23 references.

The leading underscore is a contract that says "internal, may change without
notice". When three other modules depend on it, that contract is false, and the
false version is the dangerous one: the author of chat_store.py has every reason
to believe `_chat_append` is theirs to rename, and nothing tells them otherwise.
Those eight are now public, which changes nothing at runtime and everything about
what the name promises.

Scope: cross-module imports inside the shipped package plus the two top-level
entrypoints. Tests may reach into privates — pinning internal behaviour is what a
unit test is for — and a module's own privates are its own business.
"""
import ast
import pathlib

from gitfiles import tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Modules whose imports are checked. Everything shipped and importable.
_WATCHED_PREFIXES = ("ava_bridge/",)
_WATCHED_FILES = {"phone_bridge.py", "serve.py", "ava_router.py", "ava_cli.py"}

# PRE-EXISTING debt, recorded so this guard is a ratchet rather than a cliff: it
# blocks NEW cross-module private imports today, without a 22-site rename landing
# in the same change as the guard that would police it.
#
# The eight highest-traffic names are already promoted (chat_append alone had 23
# references). The eleven `code_agent -> coder` entries that dominated this list
# are gone: both modules went with governed self-editing, which is the cheapest
# way this debt has ever been paid. What is left is one relationship:
#   * phone_bridge -> auth/chat_store (9): mostly dissolves on its own when
#     phone_bridge's routes are extracted into per-domain routers (audit C1).
#
# Shrinking this list is the work. Adding to it needs a reason in the diff.
_ALLOW: set[str] = {
    "ava_bridge/alloc/spec.py: from ..connectors import _expand",
}


def _watched() -> list[str]:
    return [f for f in tracked("*.py")
            if f.startswith(_WATCHED_PREFIXES) or f in _WATCHED_FILES]


def _private_imports(path: pathlib.Path) -> list[str]:
    """`from <other module> import _name` occurrences in this file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        # node.module is None for `from . import x`; that form imports a MODULE,
        # not a name out of one, so it is not the pattern under test.
        if node.module is None:
            continue
        for alias in node.names:
            if alias.name.startswith("_") and not alias.name.startswith("__"):
                hits.append(f"from {'.' * node.level}{node.module} import {alias.name}")
    return hits


def test_no_module_imports_another_modules_private_name() -> None:
    offenders: list[str] = []
    for rel in _watched():
        for hit in _private_imports(ROOT / rel):
            entry = f"{rel}: {hit}"
            if entry not in _ALLOW:
                offenders.append(entry)
    assert not offenders, (
        "an underscore name imported across modules is a private contract that "
        "three callers depend on — promote it to public in the module that "
        "defines it, or add a justified entry to _ALLOW here:\n  "
        + "\n  ".join(sorted(offenders))
    )
