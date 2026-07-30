"""Ava GPU voice sidecar — natural-voice TTS (Kokoro) + fast STT (Whisper), both
on the GPU.

Runs in its OWN venv (.venv-tts) because it needs CUDA torch, which the main
bridge venv deliberately does not carry. The bridge (ava_bridge/audio.py +
phone_bridge.py) calls these endpoints over localhost and falls back to CPU
(Piper for TTS, faster-whisper for STT) whenever this service is unreachable, so
voice degrades in quality/speed but never breaks. Mirrors ava-gpu.service.

Measured on one unified-memory NVIDIA box (GB10): a sentence synthesizes in ~0.3s
and a short clip transcribes in ~0.1s, against ~1.9s for Piper and ~3s for CPU
Whisper. Those are one machine's numbers, not a spec — expect different figures
on yours; the RELATIVE ordering is the point.

Endpoints:
  POST /tts   {text, voice?}                  -> audio/wav (24 kHz)
  POST /stt   body = raw s16le PCM @ 16 kHz    -> {"text": ...}
  GET  /health

Env:
  AVA_KOKORO_VOICE   TTS voice id (default af_heart)
  AVA_KOKORO_LANG    kokoro lang_code (default 'a' = American English)
  AVA_KOKORO_DEVICE  torch device; default AUTO-DETECTED (cuda|mps|xpu|cpu)
  AVA_STT_MODEL      HF Whisper id (default openai/whisper-small.en)
  AVA_STT_DEVICE     torch device; default AUTO-DETECTED (cuda|mps|xpu|cpu)
"""
import io
import os
import threading

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

SR = 24000  # Kokoro's native sample rate
STT_SR = 16000  # what the bridge sends (decode_to_pcm output)
VOICE = os.environ.get("AVA_KOKORO_VOICE", "af_heart")
LANG = os.environ.get("AVA_KOKORO_LANG", "a")


def _pick_device() -> str:
    """The best torch device actually present: cuda -> mps -> xpu -> cpu.

    Both device vars defaulted to the literal `"cuda"`, so importing this module
    on a Mac or an AMD box **raised on model load** rather than degrading — the
    GPU voice sidecar simply did not exist off NVIDIA. An explicit
    AVA_KOKORO_DEVICE / AVA_STT_DEVICE still wins, so a box that needs pinning
    can pin.
    """
    try:
        import torch
    except Exception:  # noqa: BLE001 — torch absent; caller errors more clearly
        return "cpu"
    try:
        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        xpu = getattr(torch, "xpu", None)
        if xpu is not None and xpu.is_available():
            return "xpu"
    except Exception:  # noqa: BLE001 — a probe that raises means "not usable"
        pass
    return "cpu"


_AUTO_DEVICE = _pick_device()
DEVICE = os.environ.get("AVA_KOKORO_DEVICE") or _AUTO_DEVICE
STT_MODEL = os.environ.get("AVA_STT_MODEL", "openai/whisper-small.en")
STT_DEVICE = os.environ.get("AVA_STT_DEVICE") or _AUTO_DEVICE

app = FastAPI(title="Ava GPU voice sidecar")
_pipe = None
_asr = None
_asr_lock = threading.Lock()


def _pipeline():
    """Lazily build the Kokoro TTS pipeline once (import + model load ~10s)."""
    global _pipe
    if _pipe is None:
        from kokoro import KPipeline
        _pipe = KPipeline(lang_code=LANG, device=DEVICE)
    return _pipe


def _asr_pipeline():
    """Lazily build the Whisper STT pipeline once (model load ~20s). Guarded by a
    lock so a burst of first requests loads it exactly once."""
    global _asr
    if _asr is None:
        with _asr_lock:
            if _asr is None:
                import torch
                from transformers import pipeline
                _asr = pipeline(
                    "automatic-speech-recognition", model=STT_MODEL,
                    device=STT_DEVICE,
                    # fp16 on CUDA only, deliberately. MPS fp16 has produced NaNs
                    # in some transformers/torch combinations, and a silently
                    # empty transcript is worse than being a little slower — so
                    # every non-CUDA device gets fp32 until someone measures
                    # otherwise on real hardware.
                    torch_dtype=torch.float16 if STT_DEVICE == "cuda" else torch.float32,
                )
    return _asr


@app.on_event("startup")
def _warm():
    # Pay TTS model-load + first (cudnn-autotune) synth at boot, not on the first
    # user request. Load the STT model in the background so it doesn't delay TTS
    # readiness; until it's warm the bridge just falls back to CPU Whisper.
    try:
        for _ in _pipeline()("warm up", voice=VOICE):
            pass
        print(f"[gpu-voice] TTS ready — voice={VOICE} device={DEVICE}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[gpu-voice] TTS warm failed: {e}", flush=True)

    def _warm_stt():
        try:
            _asr_pipeline()({"array": np.zeros(STT_SR, dtype=np.float32),
                             "sampling_rate": STT_SR})
            print(f"[gpu-voice] STT ready — model={STT_MODEL} device={STT_DEVICE}",
                  flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[gpu-voice] STT warm failed: {e}", flush=True)

    threading.Thread(target=_warm_stt, daemon=True).start()


class TTSReq(BaseModel):
    text: str
    voice: str | None = None


@app.get("/health")
def health():
    # `device_auto` and `device_pinned` are reported separately so `ava doctor`
    # can say WHY a box is on cpu — an auto-detected cpu means no accelerator was
    # found, a pinned cpu means someone chose it. Those need different advice.
    return {"ok": _pipe is not None, "voice": VOICE, "device": DEVICE,
            "device_auto": _AUTO_DEVICE,
            "device_pinned": bool(os.environ.get("AVA_KOKORO_DEVICE")),
            "stt_ready": _asr is not None, "stt_model": STT_MODEL,
            "stt_device": STT_DEVICE}


@app.post("/tts")
def tts(req: TTSReq):
    text = (req.text or "").strip()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    try:
        chunks = [a for _, _, a in _pipeline()(text, voice=req.voice or VOICE)]
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"synth failed: {e}"}, status_code=500)
    if not chunks:
        return JSONResponse({"error": "no audio"}, status_code=500)
    audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    buf = io.BytesIO()
    sf.write(buf, audio, SR, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")


@app.post("/stt")
async def stt(request: Request):
    """Transcribe raw s16le PCM @ 16 kHz (what the bridge already has after
    decode_to_pcm) on the GPU."""
    pcm = await request.body()
    if not pcm:
        return JSONResponse({"error": "empty audio"}, status_code=400)
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    try:
        r = _asr_pipeline()({"array": audio, "sampling_rate": STT_SR})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"transcribe failed: {e}"}, status_code=500)
    return {"text": (r.get("text") or "").strip()}
