"""Browser-audio decode + TTS (Kokoro service, Piper fallback)."""
import os
import subprocess
import tempfile

import requests

import voice_ava as va

from . import config


def decode_to_pcm(raw: bytes) -> bytes:
    """Decode arbitrary browser audio (webm/opus or mp4/aac) to 16k mono s16le."""
    fd, path = tempfile.mkstemp(prefix="ava_phone_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(config.RATE),
             "-f", "s16le", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=config.AUDIO_DECODE_TIMEOUT,
        )
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode(errors="ignore"))
        return out.stdout
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def gpu_transcribe(pcm: bytes) -> str:
    """Transcribe s16le/16 kHz PCM on the GPU voice sidecar. Raises on any failure
    so the caller can fall back to the local CPU Whisper — STT must never depend on
    this optional service being up."""
    url = os.environ.get("AVA_STT_URL",
                         os.environ.get("AVA_KOKORO_URL", "http://127.0.0.1:8129")).rstrip("/")
    r = requests.post(f"{url}/stt", data=pcm,
                      headers={"Content-Type": "application/octet-stream"},
                      timeout=float(os.environ.get("AVA_STT_TIMEOUT", "15")))
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def _kokoro_wav_bytes(text: str) -> bytes:
    """Ask the Kokoro TTS service (GPU, natural voice) for WAV bytes. Raises on
    any failure so the caller can fall back to Piper — voice must never depend on
    this optional service being up."""
    url = os.environ.get("AVA_KOKORO_URL", "http://127.0.0.1:8129").rstrip("/")
    r = requests.post(f"{url}/tts", json={"text": text},
                      timeout=float(os.environ.get("AVA_KOKORO_TIMEOUT", "15")))
    r.raise_for_status()
    if not r.content:
        raise RuntimeError("kokoro returned empty audio")
    return r.content


def tts_wav_bytes(text: str) -> bytes:
    """Return spoken WAV bytes for `text`.

    Uses the Kokoro service when AVA_TTS=kokoro (natural voice on the GPU), and
    transparently falls back to Piper if that service is unreachable or errors,
    so a stopped kokoro-tts.service degrades voice quality but never breaks it.
    """
    if not text:
        return b""
    if os.environ.get("AVA_TTS", "piper").strip().lower() == "kokoro":
        try:
            return _kokoro_wav_bytes(text)
        except Exception as e:  # noqa: BLE001 — degrade to Piper, don't fail voice
            print(f"[ava] kokoro TTS unavailable ({e}); falling back to Piper",
                  flush=True)
    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="ava_tts_")
    os.close(fd)
    try:
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = (os.path.join(config.ROOT, "bin", "piper")
                                  + ":" + env.get("LD_LIBRARY_PATH", ""))
        subprocess.run(
            [va.PIPER, "--model", va.VOICE, "--output_file", wav],
            input=text.encode(), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        with open(wav, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
