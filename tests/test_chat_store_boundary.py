"""Only ava_bridge/chat_store.py may touch state.chats.

The chat corpus is moving off a whole-file chats.json rewrite onto a real
storage engine. That is only possible while every reader goes through the store:
if three modules hold state.chats_lock and walk the dict themselves, migrating
the data leaves those callers reading an in-memory dict that is no longer
authoritative — and nothing fails, it just quietly serves stale history.

Three did. chats_api.py reached in 11 times, data_api.py 10, and
tests/test_memory.py seeded fixtures by assigning into it. All now go through
chat_store's public operations, which are written as queries ("give me the
summaries", "delete this one") rather than "hand me the dict", precisely so they
survive the engine changing underneath them.

Same shape as tests/test_path_roots.py: a static scan over `git ls-files` that
runs anywhere, needs no bridge and no AVA_HOME, and fails with instructions.
"""
import ast
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The store itself, and the module that DEFINES the attribute.
_OWNERS = {"ava_bridge/chat_store.py", "ava_bridge/state.py"}

_GUARDED = {"chats", "chats_lock"}


def _tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def test_nothing_but_chat_store_touches_state_chats() -> None:
    offenders = []
    for rel in _tracked():
        if rel in _OWNERS or rel.split("/", 1)[0] in {"overlay", "demo", "frontend"}:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # state.chats / state.chats_lock, however it is reached
            if (isinstance(node, ast.Attribute) and node.attr in _GUARDED
                    and isinstance(node.value, ast.Name) and node.value.id == "state"):
                offenders.append(f"{rel}:{node.lineno}: state.{node.attr}")

    assert not offenders, (
        "these reach past ava_bridge/chat_store.py into the raw chat corpus:\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nUse a chat_store operation instead — get/snapshot/delete/rename/"
        "summaries/usage/counts/export_json/recent_image_urls, or add one if none "
        "fits. The point is that each is expressible as a QUERY, so the storage "
        "engine can change without every caller changing with it. A test that "
        "needs an empty corpus can call chat_store.reset()."
    )
