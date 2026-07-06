"""Browser-audio decode + Piper TTS."""
import os
import subprocess
import tempfile

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


def tts_wav_bytes(text: str) -> bytes:
    """Run Piper and return WAV bytes (instead of playing on the Spark)."""
    if not text:
        return b""
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
