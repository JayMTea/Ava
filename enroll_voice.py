#!/usr/bin/env python3
"""Enroll your voice so Ava responds to you only.

Usage (after plugging in the USB mic):
    ./.venv/bin/python enroll_voice.py            # records 5 phrases from the mic
    ./.venv/bin/python enroll_voice.py --from-wav a.wav b.wav c.wav   # use files

It records several short phrases, builds an averaged voiceprint, saves it to
models/voiceprint.npy, and prints similarity stats so you can pick a threshold.
"""
import argparse
import os
import subprocess
import sys
import wave

import numpy as np

import speaker as spk

RATE = 16000
FRAME_MS = 30
FRAME_BYTES = int(RATE * FRAME_MS / 1000) * 2
MIC_DEVICE = os.environ.get("MIC_DEVICE", "default")

PHRASES = [
    "Hey Ava, this is my voice.",
    "Ava, remember how I sound.",
    "The quick brown fox jumps over the lazy dog.",
    "Today I am setting up my private assistant.",
    "Ava only answers to me.",
]


def record_seconds(seconds: float) -> bytes:
    cmd = ["arecord", "-D", MIC_DEVICE, "-f", "S16_LE", "-c", "1",
           "-r", str(RATE), "-d", str(int(seconds)), "-t", "raw", "-q"]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        data = w.readframes(n)
        ch = w.getnchannels()
        sr = w.getframerate()
    a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != RATE:  # simple linear resample
        idx = np.linspace(0, len(a) - 1, int(len(a) * RATE / sr))
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-wav", nargs="+", help="enroll from wav files instead of mic")
    ap.add_argument("--count", type=int, default=5, help="number of mic phrases")
    ap.add_argument("--seconds", type=float, default=4.0, help="seconds per phrase")
    args = ap.parse_args()

    print("Loading ECAPA speaker model (first run downloads ~80MB)...", flush=True)
    verifier = spk.SpeakerVerifier()

    embeddings = []
    if args.from_wav:
        for p in args.from_wav:
            audio = load_wav(p)
            embeddings.append(verifier.embed_audio(audio))
            print(f"  enrolled: {p}")
    else:
        print("\nWe'll record a few short phrases. Speak naturally, normal distance from the mic.\n")
        for i in range(args.count):
            phrase = PHRASES[i % len(PHRASES)]
            input(f"[{i+1}/{args.count}] Press Enter, then say:  \"{phrase}\"")
            pcm = record_seconds(args.seconds)
            if len(pcm) < FRAME_BYTES * 10:
                print("  (too short, skipping)")
                continue
            embeddings.append(verifier.embed_pcm(pcm))
            print("  captured.")

    if len(embeddings) < 2:
        print("Not enough samples to enroll. Need at least 2.", file=sys.stderr)
        sys.exit(1)

    embeddings = np.array(embeddings)
    voiceprint = embeddings.mean(axis=0)
    voiceprint /= np.linalg.norm(voiceprint)

    # intra-speaker similarity of each sample to the mean -> helps choose threshold
    sims = [spk.cosine(voiceprint, e / np.linalg.norm(e)) for e in embeddings]
    print(f"\nSamples: {len(embeddings)}")
    print(f"Self-similarity  min={min(sims):.3f}  mean={np.mean(sims):.3f}  max={max(sims):.3f}")
    suggested = max(0.25, min(sims) - 0.05)
    print(f"Suggested SPEAKER_THRESHOLD ~= {suggested:.2f}")

    spk.save_voiceprint(voiceprint)
    print(f"\nSaved voiceprint -> {spk.VOICEPRINT}")
    print("Now run:  SPEAKER_THRESHOLD={:.2f} ./run.sh".format(suggested))


if __name__ == "__main__":
    main()
