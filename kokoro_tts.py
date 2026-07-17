"""Ava GPU voice sidecar — natural-voice TTS (Kokoro) + fast STT (Whisper), both
on the GPU.

Runs in its OWN venv (.venv-tts) because it needs CUDA torch, which the main
bridge venv deliberately does not carry. The bridge (ava_bridge/audio.py +
phone_bridge.py) calls these endpoints over localhost and falls back to CPU
(Piper for TTS, faster-whisper for STT) whenever this service is unreachable, so
voice degrades in quality/speed but never breaks. Mirrors ava-gpu.service.

On this GB10, a sentence synthesizes in ~0.3s and a short clip transcribes in
~0.1s (vs ~1.9s Piper / ~3s CPU Whisper).

Endpoints:
  POST /tts   {text, voice?}                  -> audio/wav (24 kHz)
  POST /stt   body = raw s16le PCM @ 16 kHz    -> {"text": ...}
  GET  /health

Env:
  AVA_KOKORO_VOICE   TTS voice id (default af_heart)
  AVA_KOKORO_LANG    kokoro lang_code (default 'a' = American English)
  AVA_KOKORO_DEVICE  'cuda' (default) or 'cpu'
  AVA_STT_MODEL      HF Whisper id (default openai/whisper-small.en)
  AVA_STT_DEVICE     'cuda' (default) or 'cpu'
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
DEVICE = os.environ.get("AVA_KOKORO_DEVICE", "cuda")
STT_MODEL = os.environ.get("AVA_STT_MODEL", "openai/whisper-small.en")
STT_DEVICE = os.environ.get("AVA_STT_DEVICE", "cuda")

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
    return {"ok": _pipe is not None, "voice": VOICE, "device": DEVICE,
            "stt_ready": _asr is not None, "stt_model": STT_MODEL}


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
