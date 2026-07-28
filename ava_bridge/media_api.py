"""Media and job routes — uploads, renders, upscales, and job/turn polling.

Cookie-gated. Covers the whole lifecycle of a piece of generated or uploaded
media: /api/upload takes a file in, /api/generate and /api/upscale start work,
/api/job/{id} and /api/turn/{tid} report progress, /api/jobs and /api/turns list
them, and /thumb/{name} serves the cheap inline preview.

Two things here are load-bearing rather than incidental:

_store_upload is a NAMED SYNC HELPER handed to run_in_threadpool, not inline
work. /api/upload is an `async def` route, and it once ran `soffice --headless`
(a 150-second ceiling) directly on the event loop — one user uploading one .docx
froze the whole server, every SSE stream and the login gate with it. It never
needed load to bite; it bit on the second concurrent request.
tests/test_no_blocking_routes.py enforces the pattern repo-wide.

/thumb/{name} imports ensure_thumbnail inside the handler because thumbnailing
is best-effort: without Pillow it returns None and the route serves the full
image instead. That degradation is deliberate; what is not deliberate is doing
it silently, which is why gpu_jobs warns once when Pillow is missing.
"""
import os
import uuid
from typing import List

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import dashboard, memory_store, state
from .chat_store import chat_append
from .config import (IMAGE_EXTS, MAX_DOC_CHARS, MAX_UPLOAD_BYTES, MEDIA_DIR,
                     UPLOAD_DIR)
from .documents import extract_text, safe_name
from .gpu_jobs import cancel_job, start_image_job, start_upscale_job

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

@router.get("/thumb/{name}")
def media_thumb(name: str, w: int = 1024):
    """Small WebP thumbnail of a generated image for fast inline chat display —
    the full 4K PNG is 16 MB+, the bubble shows it at ~500px. Lazy + disk-cached
    (immutable: a filename's bytes never change); falls back to the full image
    if thumbnailing isn't possible. See gpu_jobs.ensure_thumbnail."""
    from ava_bridge.gpu_jobs import ensure_thumbnail
    thumb = ensure_thumbnail(name, w)
    if thumb:
        return FileResponse(thumb, media_type="image/webp",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})
    full = os.path.join(MEDIA_DIR, os.path.basename(name))
    if os.path.isfile(full):
        return FileResponse(full)
    return JSONResponse({"error": "not found"}, status_code=404)

@router.get("/api/jobs")
async def api_jobs(status: str | None = None, kind: str | None = None,
                   limit: int = 100):
    return await run_in_threadpool(dashboard.jobs_list, status, kind, limit)

@router.get("/api/turns")
async def api_turns(limit: int = 50, active: bool = False):
    return await run_in_threadpool(dashboard.turns_list, limit, active)

@router.post("/api/generate")
async def generate(prompt: str = Form(...), width: int = Form(1024),
                   height: int = Form(1024), steps: int = Form(28),
                   chat_id: str = Form(""), chat_text: str = Form("")):
    """Directly start an image render from a text prompt (no voice).

    GOVERNANCE NOTE: this is the one sanctioned direct path that bypasses the
    OpenClaw agent. All conversational turns (/api/talk, /api/talk-text) route
    through Ava-the-agent (ask_openclaw) so persona, memory and tool-policies
    apply, and agent-initiated images go via her `run_gpu_job` MCP tool. This
    endpoint is a deliberate UX fast-path for an EXPLICIT user "generate" action
    (a button, not a sentence): there is no reasoning to govern, and it stays on
    the same local host the GPU service behind the same egress boundary. Keep this the
    exception — do not add conversational logic here; send that through the agent.
    """
    prompt = prompt.strip()
    if not prompt:
        return JSONResponse({"error": "empty prompt"}, status_code=400)
    if chat_id:
        chat_append(chat_id, "user", (chat_text or prompt).strip())
    # chat_id rides the job: the bridge persists the outcome (image or coded
    # error) when the render ends — the client only paints progress.
    job_id = start_image_job(prompt, chat_id=chat_id or None,
                             width=width, height=height, steps=steps)
    return {"job": {"id": job_id, "kind": "image", "prompt": prompt}}

@router.post("/api/upscale")
async def upscale(filename: str = Form(...), chat_id: str = Form(""),
                  caption: str = Form("")):
    """Optionally upscale a generated image ~4x (the refiner) to ~4K."""
    name = os.path.basename(filename.strip())
    if not name or not name.lower().endswith(".png"):
        return JSONResponse({"error": "bad filename"}, status_code=400)
    src = os.path.join(MEDIA_DIR, name)
    if not os.path.isfile(src):
        return JSONResponse({"error": "image not found"}, status_code=404)
    job_id = start_upscale_job(src, chat_id=chat_id.strip() or None,
                               caption=caption.strip() or None)
    return {"job": {"id": job_id, "kind": "upscale"}}

@router.get("/api/job/{job_id}")
def job_status(job_id: str):
    with state.jobs_lock:
        job = state.jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return job

@router.post("/api/job/{job_id}/cancel")
def job_cancel(job_id: str):
    ok = cancel_job(job_id)
    if not ok:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    with state.jobs_lock:
        job = state.jobs.get(job_id)
    return {"ok": True, "job": job}

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

