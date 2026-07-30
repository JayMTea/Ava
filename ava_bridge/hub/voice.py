"""Setup -> Voice panel: voiceprint enrolment and the speaker gate.

Uploading clips enrols the biometric voiceprint the speaker gate compares
against. The clip limits live here with the route that enforces them, and both
are deliberately small: SECURITY.md is explicit that the gate is a privacy and
convenience FILTER, not an authentication factor — it is text-independent cosine
matching with no liveness detection, and it fails open when voice is enabled
with nothing enrolled. The session cookie remains the auth boundary.
"""
from fastapi import APIRouter, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import features, settings
from .. import voice_enroll

router = APIRouter()

# --------------------------------------------------------------------------- #
# Voice — status / enroll from browser recordings / test similarity
# --------------------------------------------------------------------------- #
@router.get("/voice/status")
def voice_status():
    st = voice_enroll.status()
    st["enabled"] = features.enabled("voice")
    return st

# Upload bounds: recordings are short mic clips — a minute of webm/opus is well
# under 1 MB, so 25 MB/clip and 8 clips is generous while stopping OOM abuse.
_MAX_CLIP_BYTES = 25 * 1024 * 1024

_MAX_CLIPS = 8

async def _read_clip(f: UploadFile) -> bytes | None:
    """Read one upload with a hard size cap (None = too large)."""
    data = await f.read(_MAX_CLIP_BYTES + 1)
    return None if len(data) > _MAX_CLIP_BYTES else data

@router.post("/voice/enroll")
async def voice_enroll_ep(files: list[UploadFile]):
    """Build + save the voiceprint from uploaded recordings (any format the
    browser produces — decoded via ffmpeg). Embedding runs in a worker thread."""
    if len(files) > _MAX_CLIPS:
        return JSONResponse({"ok": False, "error": f"too many clips (max {_MAX_CLIPS})"},
                            status_code=413)
    clips = []
    for f in files:
        data = await _read_clip(f)
        if data is None:
            return JSONResponse({"ok": False, "error": "a clip exceeds 25 MB"},
                                status_code=413)
        clips.append(data)
    if not clips or all(len(c) == 0 for c in clips):
        return JSONResponse({"ok": False, "error": "no audio uploaded"}, status_code=400)
    res = await run_in_threadpool(voice_enroll.enroll, clips)
    return res if res.get("ok") else JSONResponse(res, status_code=422)

@router.post("/voice/test")
async def voice_test_ep(file: UploadFile):
    """Similarity of one clip against the enrolled voiceprint (gate preview)."""
    clip = await _read_clip(file)
    if clip is None:
        return JSONResponse({"ok": False, "error": "clip exceeds 25 MB"}, status_code=413)
    if not clip:
        return JSONResponse({"ok": False, "error": "no audio uploaded"}, status_code=400)
    res = await run_in_threadpool(voice_enroll.test, clip)
    return res if res.get("ok") else JSONResponse(res, status_code=422)

@router.post("/voice/threshold")
def voice_threshold(value: float):
    """Set the speaker-gate threshold (the Hub's 'apply suggested threshold').
    Persists to ava.yaml; a restart applies it to the live gate."""
    if not (0.2 <= value <= 0.95):
        return JSONResponse({"ok": False, "error": "threshold must be 0.2–0.95"},
                            status_code=400)
    try:
        settings.save_patch({"voice": {"threshold": round(float(value), 2)}})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    return {"ok": True, "restart_required": True,
            "env_override": settings.env_override("AVA_PHONE_THRESHOLD")}



@router.post("/voice/delete")
async def voice_delete():
    """Destroy the enrolled voiceprint and everything derived from it.

    POST rather than DELETE to match this panel's other verbs (/voice/enroll,
    /voice/test, /voice/threshold) — consistency inside one surface beats REST
    purity here.

    Returns the receipt from `voice_enroll.delete()`, which lists absolute paths
    so the owner can verify by hand rather than trusting this response. That is
    the point: `GET /voice/status` reporting `enrolled: false` is the tool's own
    report about itself, and this project's doctrine is not to accept those.
    """
    res = await run_in_threadpool(voice_enroll.delete)
    return res if res.get("ok") else JSONResponse(res, status_code=500)
