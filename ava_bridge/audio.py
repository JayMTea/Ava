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


def _piper_bin() -> str | None:
    """Where Piper actually is, if anywhere.

    Three sources, most explicit first. The vendored path is LAST on purpose: it
    is gitignored (`.gitignore`), so it exists on the maintainer's box and in NO
    fork — while `AVA_TTS` defaulted to `"piper"`. Every fork therefore shipped
    with its default TTS engine pointing at a file that was not there.
    """
    import shutil
    explicit = (os.environ.get("AVA_PIPER_BIN") or "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    onpath = shutil.which("piper")          # the `piper-tts` pip package's script
    if onpath:
        return onpath
    vendored = os.path.join(config.ROOT, "bin", "piper", "piper")
    return vendored if os.path.isfile(vendored) else None


def _resolve_tts() -> str:
    """Which TTS engine to use: the single decision point.

    `AVA_TTS` used to default to the literal `"piper"`, so the default engine was
    one that most installs did not have (see `_piper_bin`). It now defaults to
    `auto`, which picks the best engine actually present — and on macOS that is
    `say`, which is built in, needs no install and sounds good, so a Mac has
    working speech before anyone runs pip.
    """
    want = (os.environ.get("AVA_TTS") or "auto").strip().lower()
    if want and want != "auto":
        return want
    if _kokoro_reachable():
        return "kokoro"
    if _piper_bin():
        return "piper"
    import shutil
    import sys
    if sys.platform == "darwin" and shutil.which("say"):
        return "say"
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return "espeak"
    return "none"


def _kokoro_reachable() -> bool:
    url = os.environ.get("AVA_KOKORO_URL", "http://127.0.0.1:8129").rstrip("/")
    try:
        return requests.get(f"{url}/health", timeout=0.5).status_code < 500
    except Exception:  # noqa: BLE001 — unreachable is a normal answer
        return False


def tts_wav_bytes(text: str) -> bytes:
    """Return spoken WAV bytes for `text`.

    Engine order comes from `_resolve_tts()`; whichever is chosen, a failure
    degrades to the next rather than breaking voice — a stopped kokoro service
    should cost quality, never speech.
    """
    if not text:
        return b""
    engine = _resolve_tts()

    if engine == "kokoro":
        try:
            return _kokoro_wav_bytes(text)
        except Exception as e:  # noqa: BLE001 — degrade, don't fail voice
            print(f"[ava] kokoro TTS unavailable ({e}); falling back", flush=True)
            engine = "piper" if _piper_bin() else _resolve_tts()

    if engine == "piper":
        piper = _piper_bin()
        if piper:
            return _run_to_wav(
                [piper, "--model", va.VOICE, "--output_file", "{wav}"],
                text, extra_env=_piper_env(piper))
        engine = "none"

    if engine == "say":
        # macOS built-in. `-o out.wav --data-format=LEI16@22050` writes a real
        # RIFF WAV rather than the default AIFF, which the browser cannot play.
        return _run_to_wav(["say", "-o", "{wav}",
                            "--data-format=LEI16@22050", text], None)

    if engine == "espeak":
        import shutil
        exe = shutil.which("espeak-ng") or shutil.which("espeak")
        return _run_to_wav([exe, "-w", "{wav}", text], None)

    # No engine. Return silence rather than raising: `phone_bridge` calls this
    # inline while building the /api/talk response, so an exception here would
    # take the assistant's TEXT reply down with the audio. Text is the payload and
    # speech is the enhancement, so the caller gets an empty clip and the reply.
    _warn_no_tts()
    return b""


_warned_no_tts = False


def _warn_no_tts() -> None:
    """Log the missing-engine advice once, not once per turn."""
    global _warned_no_tts
    if _warned_no_tts:
        return
    _warned_no_tts = True
    print("[ava] no TTS engine available, so replies will be text-only. Install "
          "one: `pip install piper-tts`, or run the kokoro sidecar; on macOS "
          "nothing is needed (`say` is built in). Pin a choice with "
          "AVA_TTS=kokoro|piper|say|espeak.", flush=True)


def _piper_env(piper: str) -> dict:
    """Piper's vendored build needs its own libs on the loader path.

    Only for the vendored layout — a pip-installed `piper` carries its ONNX
    runtime in the wheel, so touching the loader path there would be wrong.
    `DYLD_LIBRARY_PATH` is the macOS spelling; a vendored macOS build is not
    something Ava ships, but naming it costs nothing and avoids a silent
    dlopen failure for anyone who builds one.
    """
    lib = os.path.dirname(piper)
    if not os.path.isfile(os.path.join(lib, "libonnxruntime.so.1.14.1")) and \
            not os.path.isdir(os.path.join(lib, "espeak-ng-data")):
        return {}
    import sys
    var = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
    return {var: lib + ":" + os.environ.get(var, "")}


def _run_to_wav(argv: list[str], stdin_text: str | None,
                extra_env: dict | None = None) -> bytes:
    """Run a TTS command that writes a WAV to a temp path, and return its bytes.

    `"{wav}"` in argv is replaced with the temp path — one helper so each engine
    is a list of arguments rather than its own copy of the tempfile dance.
    """
    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="ava_tts_")
    os.close(fd)
    try:
        env = dict(os.environ)
        env.update(extra_env or {})
        subprocess.run(
            [a.replace("{wav}", wav) for a in argv],
            input=stdin_text.encode() if stdin_text is not None else None,
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True,
        )
        with open(wav, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
