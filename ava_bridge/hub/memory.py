"""Setup -> Memory panel: what Ava remembers, and forgetting it.

Reads and edits the memory store's entries. Deletions are audited, because
"forget this" is a claim the owner should be able to verify after the fact.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import audit, config
from .. import memory_store

router = APIRouter()

# --------------------------------------------------------------------------- #
# Memory — the governed long-term store (ava_bridge/memory_store.py).
# The owner can read, correct, and delete everything Ava remembers; recalls
# that influenced a turn are in the audit ledger (kind=memory_recall).
# --------------------------------------------------------------------------- #
@router.get("/memory/export")
def memory_export():
    """The whole store as a JSON download — your data leaves in one click."""
    return JSONResponse(
        memory_store.export_all(),
        headers={"Content-Disposition": 'attachment; filename="ava-memory.json"'})

@router.get("/memory")
def memory_list(q: str = "", kind: str = "", limit: int = 100, offset: int = 0):
    """List (newest first) or free-text search the memory store."""
    if kind not in ("", "fact", "doc"):
        return JSONResponse({"error": "kind must be fact|doc"}, status_code=400)
    items = memory_store.list_items(kind=kind, query=q.strip(),
                                    limit=limit, offset=offset)
    return {"items": items, "counts": memory_store.counts(),
            "enabled": config.MEMORY_ENABLED}

@router.post("/memory")
async def memory_add(request: Request):
    """Add a manual fact: {"text": ...}. Manual facts rank like any other."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "empty text"}, status_code=400)
    mid = memory_store.add("fact", text, source="manual")
    if mid is None:
        return JSONResponse({"ok": False, "error": "could not write memory store"},
                            status_code=500)
    audit.record("memory_edit", action="add", id=mid)
    return {"ok": True, "id": mid}

@router.post("/memory/{mid}")
async def memory_update(mid: int, request: Request):
    """Edit one item: {"text": ...} and/or {"pinned": bool}."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    text = body.get("text")
    pinned = body.get("pinned")
    if text is None and pinned is None:
        return JSONResponse({"ok": False, "error": "nothing to update"},
                            status_code=400)
    ok = memory_store.update_item(
        mid, text=(str(text) if text is not None else None),
        pinned=(bool(pinned) if pinned is not None else None))
    if not ok:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    audit.record("memory_edit", action="update", id=mid)
    return {"ok": True}

@router.post("/memory/{mid}/delete")
def memory_delete(mid: int):
    if not memory_store.delete_item(mid):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    audit.record("memory_edit", action="delete", id=mid)
    return {"ok": True}

