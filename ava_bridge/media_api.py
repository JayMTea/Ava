"""Media and turn routes — uploads and turn polling.

Cookie-gated. Covers the whole lifecycle of an uploaded file: /api/upload takes
it in, and /api/turn/{tid} and /api/turns report on the chat turns it rides.

One thing here is load-bearing rather than incidental:

_store_upload is a NAMED SYNC HELPER handed to run_in_threadpool, not inline
work. /api/upload is an `async def` route, and it once ran `soffice --headless`
(a 150-second ceiling) directly on the event loop — one user uploading one .docx
froze the whole server, every SSE stream and the login gate with it. It never
needed load to bite; it bit on the second concurrent request.
tests/test_no_blocking_routes.py enforces the pattern repo-wide.
"""
import os
import uuid
from typing import List

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import dashboard, memory_store, state
from .config import IMAGE_EXTS, MAX_DOC_CHARS, MAX_UPLOAD_BYTES, UPLOAD_DIR
from .documents import extract_text, safe_name

router = APIRouter()

# Upload types we know how to store/extract; anything else is refused so the
# upload dir can't be used to stash executables, keys, or other arbitrary files.
_ALLOWED_UPLOAD_EXTS = {
    ".pdf", ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".rtf",
    ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic",
}


def _store_upload(raw: bytes, aid: str, safe: str, ext: str) -> dict:
    """Blocking half of /api/upload: disk write, text extraction, memory index.

    Split out so it can run in a threadpool. `extract_text` shells out to
    `soffice --headless` for office documents with a 150s ceiling, and running
    that on the event loop froze the whole process for its duration \u2014 every SSE
    stream, every /apps/{cid} proxy hop, and the login gate with it. One user
    uploading one .docx was enough; it did not need load to bite.
    """
    stored = f"{aid}_{safe}"
    dest = os.path.join(UPLOAD_DIR, stored)
    with open(dest, "wb") as f:
        f.write(raw)
    is_image = ext in IMAGE_EXTS
    text = (extract_text(dest, ext) or "").strip()
    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS] + "\n\u2026[truncated]"
    rec = {"id": aid, "filename": safe,
           "kind": "image" if is_image else "document",
           "url": f"/uploads/{stored}" if is_image else None,
           "chars": len(text), "ocr": bool(is_image and text), "text": text}
    # Long-term memory: index document text so it stays searchable in
    # future conversations (not just this message). Images skipped unless
    # OCR found real text.
    if not is_image or rec["ocr"]:
        memory_store.index_document(aid, safe, text)
    return rec

@router.get("/api/turns")
async def api_turns(limit: int = 50, active: bool = False):
    return await run_in_threadpool(dashboard.turns_list, limit, active)

@router.get("/api/turn/{tid}")
def turn_status(tid: str):
    with state.turns_lock:
        t = state.turns.get(tid)
        if not t:
            return JSONResponse({"error": "unknown turn"}, status_code=404)
        return dict(t)

@router.post("/api/upload")
async def upload(files: List[UploadFile] = File(...)):
    """Accept document/image uploads, extract text, stash for the next turn."""
    out = []
    for uf in files:
        raw = await uf.read()
        if not raw:
            continue
        if len(raw) > MAX_UPLOAD_BYTES:
            out.append({"error": f"{uf.filename}: too large "
                                 f"(max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"})
            continue
        aid = uuid.uuid4().hex[:12]
        safe = safe_name(uf.filename)
        ext = os.path.splitext(safe)[1].lower()
        if ext not in _ALLOWED_UPLOAD_EXTS:
            out.append({"error": f"{uf.filename}: file type '{ext or '?'}' not allowed"})
            continue
        # Validation above is cheap and stays on the loop; everything that
        # touches disk, a subprocess or SQLite goes to a worker thread.
        rec = await run_in_threadpool(_store_upload, raw, aid, safe, ext)
        with state.attachments_lock:
            state.attachments[aid] = rec
        out.append({k: rec[k] for k in ("id", "filename", "kind", "url", "chars", "ocr")})
    return {"attachments": out}

