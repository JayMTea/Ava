#!/usr/bin/env python3
"""Local voice loop for Ava on the DGX Spark.

Pipeline:  USB mic --(arecord)--> energy VAD --> faster-whisper (STT, CPU)
           --> Ava (vLLM open-model 30B, port 8002) --> Piper (TTS) --> aplay/pw-play (TV HDMI)

Everything runs locally. Whisper stays on CPU so it never competes with the
Omni model for GPU memory.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np
import requests

import speaker as spk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from ava_bridge import settings as _settings
    _ASSISTANT = _settings.brand_name()
    _OWNER = _settings.owner_name() or "the user"
    _OWNER_ADDR = _settings.owner_name()
    _HARDWARE = _settings.owner_hardware()
except Exception:  # noqa: BLE001 — voice loop must run even without the config layer
    _ASSISTANT, _OWNER, _OWNER_ADDR, _HARDWARE = "Ava", "the user", "", "your local hardware"

_ADDR = f" {_OWNER_ADDR}" if _OWNER_ADDR else ""

# ---- Config (env overridable) -------------------------------------------------
AVA_URL = os.environ.get("AVA_URL", "http://localhost:8002/v1/chat/completions")
AVA_MODEL = os.environ.get("AVA_MODEL", "nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small.en")

MIC_DEVICE = os.environ.get("MIC_DEVICE", "default")   # arecord -D ... (e.g. plughw:1,0)
PLAYER = os.environ.get("PLAYER", "aplay")              # aplay | pw-play | paplay
OUT_DEVICE = os.environ.get("OUT_DEVICE", "")           # aplay -D ... (e.g. plughw:0,3 for HDMI 0)

PIPER = os.path.join(HERE, "bin", "piper", "piper")
VOICE = os.environ.get("VOICE", os.path.join(HERE, "models", "en_US-amy-medium.onnx"))

WAKE_WORD = os.environ.get("WAKE_WORD", "").strip().lower()  # optional, e.g. "ava"

# Speaker verification: if models/voiceprint.npy exists, respond to that voice only.
# Set SPEAKER_THRESHOLD=0 to disable. Higher = stricter (typical 0.30-0.45).
SPEAKER_THRESHOLD = float(os.environ.get("SPEAKER_THRESHOLD", "0.35"))

# Audio / VAD params
RATE = 16000
FRAME_MS = 30
FRAME_BYTES = int(RATE * FRAME_MS / 1000) * 2          # 16-bit mono
SILENCE_HANG_MS = 800                                  # stop after this much trailing silence
MIN_SPEECH_MS = 300                                    # ignore blips shorter than this
MAX_UTTERANCE_S = 20                                   # hard cap per utterance

PERSONA = (
    f"You are {_ASSISTANT}, the personal, always-on AI assistant for {_OWNER}, running "
    f"locally and privately on {_HARDWARE}. Your name is {_ASSISTANT}; always "
    f"refer to yourself as {_ASSISTANT} and never as OpenClaw, a runtime, or a language "
    "model. Be warm, concise, proactive, and capable — a private on-device "
    "assistant in the spirit of Siri. Answer in one to three spoken sentences "
    "unless more detail is explicitly requested; avoid markdown, lists, and "
    "code blocks since your reply will be read aloud."
)


def rms(frame: bytes) -> float:
    if not frame:
        return 0.0
    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    if a.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(a * a)))


def open_mic() -> subprocess.Popen:
    cmd = ["arecord", "-D", MIC_DEVICE, "-f", "S16_LE", "-c", "1", "-r", str(RATE), "-t", "raw", "-q"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def calibrate(proc: subprocess.Popen, seconds: float = 0.6) -> float:
    """Sample ambient noise to set a speech threshold."""
    frames = max(1, int(seconds * 1000 / FRAME_MS))
    vals = []
    for _ in range(frames):
        chunk = proc.stdout.read(FRAME_BYTES)
        if not chunk:
            break
        vals.append(rms(chunk))
    noise = float(np.median(vals)) if vals else 200.0
    thresh = max(noise * 3.0, 350.0)
    return thresh


def capture_utterance(proc: subprocess.Popen, threshold: float) -> bytes:
    """Block until speech starts, then record until trailing silence."""
    hang_frames = int(SILENCE_HANG_MS / FRAME_MS)
    min_speech_frames = int(MIN_SPEECH_MS / FRAME_MS)
    max_frames = int(MAX_UTTERANCE_S * 1000 / FRAME_MS)

    voiced = bytearray()
    started = False
    silence_run = 0
    speech_run = 0

    while True:
        chunk = proc.stdout.read(FRAME_BYTES)
        if not chunk:
            break
        level = rms(chunk)
        is_speech = level > threshold

        if not started:
            if is_speech:
                speech_run += 1
                voiced += chunk
                if speech_run >= 2:          # require a couple frames to start
                    started = True
            else:
                speech_run = 0
                voiced.clear()
            continue

        voiced += chunk
        if is_speech:
            silence_run = 0
        else:
            silence_run += 1
            if silence_run >= hang_frames:
                break
        if len(voiced) // FRAME_BYTES >= max_frames:
            break

    if len(voiced) // FRAME_BYTES < min_speech_frames:
        return b""
    return bytes(voiced)


def to_wav(pcm: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="ava_in_")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return path


def transcribe(model, pcm: bytes) -> str:
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=False)
    return " ".join(s.text for s in segments).strip()


def ask_ava(history: list, user_text: str) -> str:
    messages = [{"role": "system", "content": PERSONA}] + history + [
        {"role": "user", "content": user_text}
    ]
    payload = {
        "model": AVA_MODEL,
        "messages": messages,
        "temperature": 1.0,
        "top_p": 0.95,
        "max_tokens": 400,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = requests.post(AVA_URL, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def speak(text: str):
    if not text:
        return
    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="ava_out_")
    os.close(fd)
    try:
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = os.path.join(HERE, "bin", "piper") + ":" + env.get("LD_LIBRARY_PATH", "")
        subprocess.run(
            [PIPER, "--model", VOICE, "--output_file", wav],
            input=text.encode(), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        if PLAYER == "aplay":
            cmd = ["aplay", "-q"] + (["-D", OUT_DEVICE] if OUT_DEVICE else []) + [wav]
        else:  # pw-play / paplay
            cmd = [PLAYER] + (["--device", OUT_DEVICE] if OUT_DEVICE and PLAYER == "pw-play" else []) + [wav]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Ava local voice loop")
    ap.add_argument("--mode", choices=["listen", "ptt"], default="listen",
                    help="listen = hands-free VAD; ptt = press Enter to talk")
    ap.add_argument("--no-greeting", action="store_true")
    args = ap.parse_args()

    print(f"[Ava] loading Whisper '{WHISPER_MODEL}' on CPU...", flush=True)
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    print("[Ava] STT ready.", flush=True)

    verifier = None
    voiceprint = spk.load_voiceprint()
    if voiceprint is not None and SPEAKER_THRESHOLD > 0:
        print("[Ava] loading speaker verification (your-voice-only)...", flush=True)
        verifier = spk.SpeakerVerifier()
        print(f"[Ava] speaker gate ON (threshold={SPEAKER_THRESHOLD}).", flush=True)
    else:
        print("[Ava] speaker gate OFF (no voiceprint enrolled).", flush=True)

    if not args.no_greeting:
        speak("Ava is online and listening.")

    history: list = []
    proc = None
    threshold = 350.0
    try:
        if args.mode == "listen":
            proc = open_mic()
            threshold = calibrate(proc)
            print(f"[Ava] listening (speech threshold={threshold:.0f}). Ctrl-C to quit.", flush=True)

        while True:
            if args.mode == "ptt":
                input("\n[Ava] press Enter, then speak (Ctrl-C to quit)...")
                proc = open_mic()
                threshold = calibrate(proc)

            pcm = capture_utterance(proc, threshold)
            if args.mode == "ptt" and proc:
                proc.terminate(); proc = None

            if not pcm:
                continue

            if verifier is not None:
                sim = spk.cosine(voiceprint, verifier.embed_pcm(pcm))
                if sim < SPEAKER_THRESHOLD:
                    print(f"[Ava] ignored: voice {sim:.2f} < {SPEAKER_THRESHOLD}", flush=True)
                    continue

            text = transcribe(model, pcm)
            if not text:
                continue
            print(f"\nYou: {text}", flush=True)

            if WAKE_WORD and WAKE_WORD not in text.lower():
                continue
            if text.lower().strip(" .!?") in {"stop", "quit", "exit", "goodbye ava", "bye ava"}:
                speak(f"Goodbye{_ADDR}.")
                break

            try:
                reply = ask_ava(history, text)
            except Exception as e:  # noqa: BLE001  (surface the failure aloud)
                print(f"[Ava] Ava error: {e}", flush=True)
                speak(f"Sorry{_ADDR}, I could not reach my model just now.")
                continue

            print(f"Ava: {reply}", flush=True)
            history += [{"role": "user", "content": text},
                        {"role": "assistant", "content": reply}]
            history = history[-12:]  # keep last 6 turns
            speak(reply)

    except KeyboardInterrupt:
        print("\n[Ava] bye.", flush=True)
    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    main()
