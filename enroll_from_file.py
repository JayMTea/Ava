#!/usr/bin/env python3
"""Build Ava's "my voice only" voiceprint from a recording (any audio format).

You record ~45-60s of your natural speech on your PC, drop the file into
~/projects/Ava/enroll/, then run:

    ./.venv/bin/python enroll_from_file.py enroll/my_voice.m4a

It decodes via ffmpeg, splits into short windows, builds an averaged ECAPA
voiceprint, prints quality stats, and saves models/voiceprint.npy so Ava will
respond to your voice only.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

import speaker as spk

RATE = 16000
WIN_S = 4.0          # window length for each embedding
HOP_S = 2.0          # hop between windows (overlap)
MIN_RMS = 0.01       # drop near-silent windows


def decode_to_pcm(path: str) -> np.ndarray:
    """Decode any audio file to 16k mono float32 via ffmpeg."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(RATE),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if out.returncode != 0:
        sys.exit(f"ffmpeg failed on {path}:\n{out.stderr.decode(errors='ignore')}")
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def windows(audio: np.ndarray):
    win = int(WIN_S * RATE)
    hop = int(HOP_S * RATE)
    if len(audio) <= win:
        yield audio
        return
    for start in range(0, len(audio) - win + 1, hop):
        yield audio[start:start + win]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="one or more recordings (m4a/wav/mp3/...) ")
    ap.add_argument("--threshold-margin", type=float, default=0.05,
                    help="how far below the weakest kept sample to set the threshold")
    ap.add_argument("--trim", type=float, default=1.0,
                    help="drop windows more than N std-devs below the mean "
                         "(outlier rejection; 0 = keep all windows)")
    args = ap.parse_args()

    for f in args.files:
        if not os.path.exists(f):
            sys.exit(f"file not found: {f}")

    print("Loading ECAPA speaker model (first run downloads ~80MB)...", flush=True)
    verifier = spk.SpeakerVerifier()

    embeddings = []
    total_secs = 0.0
    for f in args.files:
        audio = decode_to_pcm(f)
        total_secs += len(audio) / RATE
        for w in windows(audio):
            if float(np.sqrt(np.mean(w * w))) < MIN_RMS:
                continue  # skip silence
            embeddings.append(verifier.embed_audio(np.ascontiguousarray(w)))

    if len(embeddings) < 2:
        sys.exit("Not enough speech captured. Record at least ~10 seconds of talking.")

    embeddings = np.array(embeddings)
    # Unit-normalize every window once so similarities are clean cosines.
    embeddings = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)

    def centroid(embs):
        c = embs.mean(axis=0)
        return c / np.linalg.norm(c)

    voiceprint = centroid(embeddings)
    sims = np.array([spk.cosine(voiceprint, e) for e in embeddings])

    # Outlier rejection: drop windows that sit far below the mean (breaths,
    # noise, atypical segments), then rebuild a tighter centroid.
    kept = embeddings
    dropped = 0
    if args.trim > 0 and len(embeddings) >= 4:
        cutoff = sims.mean() - args.trim * sims.std()
        mask = sims >= cutoff
        if mask.sum() >= 2 and mask.sum() < len(embeddings):
            kept = embeddings[mask]
            dropped = int((~mask).sum())
            voiceprint = centroid(kept)
            sims = np.array([spk.cosine(voiceprint, e) for e in kept])

    print(f"\nAudio used: {total_secs:.0f}s  ->  {len(embeddings)} voice windows"
          + (f"  ({dropped} outlier(s) dropped)" if dropped else ""))
    print(f"Consistency  min={sims.min():.3f}  mean={sims.mean():.3f}  max={sims.max():.3f}")
    suggested = max(0.25, float(sims.min()) - args.threshold_margin)

    spk.save_voiceprint(voiceprint)
    print(f"\n✓ Saved your voiceprint -> {spk.VOICEPRINT}")
    print(f"  Suggested SPEAKER_THRESHOLD = {suggested:.2f}")
    if sims.mean() < 0.6:
        print("  NOTE: consistency is a bit low — a cleaner/longer recording would help.")


if __name__ == "__main__":
    main()
