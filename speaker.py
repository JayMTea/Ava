#!/usr/bin/env python3
"""Speaker verification for Ava — ECAPA-TDNN voiceprints (CPU).

Used to make Ava respond to one enrolled voice (the owner) only.
"""
import os
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ECAPA_DIR = os.path.join(HERE, "models", "ecapa")
VOICEPRINT = os.path.join(HERE, "models", "voiceprint.npy")
RATE = 16000


class SpeakerVerifier:
    def __init__(self):
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        import torch
        from speechbrain.inference import EncoderClassifier
        self._torch = torch
        self.model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=ECAPA_DIR,
            run_opts={"device": "cpu"},
        )

    def embed_pcm(self, pcm: bytes) -> np.ndarray:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return self.embed_audio(audio)

    def embed_audio(self, audio: np.ndarray) -> np.ndarray:
        sig = self._torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0)
        with self._torch.no_grad():
            emb = self.model.encode_batch(sig).squeeze().cpu().numpy()
        n = np.linalg.norm(emb)
        return emb / n if n > 0 else emb


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))  # inputs already L2-normalized


def load_voiceprint(path: str = VOICEPRINT):
    return np.load(path) if os.path.exists(path) else None


def save_voiceprint(emb: np.ndarray, path: str = VOICEPRINT):
    np.save(path, emb)
