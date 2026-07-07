#!/usr/bin/env python3
"""Speaker verification for Ava — ECAPA-TDNN voiceprints (CPU).

Used to make Ava respond to one enrolled voice (the owner) only.
"""
import os
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))


def _models_dir() -> str:
    """The persistent model store: $AVA_HOME/models (the Docker /data volume),
    falling back to the repo's models/ for bare-metal installs where AVA_HOME
    is the repo. Resolved via settings when importable so speaker.py stays
    usable as a standalone script."""
    try:
        from ava_bridge import settings
        return settings.models_dir()
    except Exception:  # noqa: BLE001 — standalone script / minimal env
        return os.path.join(os.environ.get("AVA_HOME", HERE), "models")


_LEGACY_VOICEPRINT = os.path.join(HERE, "models", "voiceprint.npy")
ECAPA_DIR = os.path.join(_models_dir(), "ecapa")
VOICEPRINT = os.path.join(_models_dir(), "voiceprint.npy")
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
    """Load the enrolled voiceprint; migrates a pre-existing legacy repo-local
    voiceprint (models/ under the CODE dir) into the persistent store so a
    Docker rebuild or AVA_HOME move never silently loses the enrollment."""
    if os.path.exists(path):
        return np.load(path)
    if path == VOICEPRINT and os.path.exists(_LEGACY_VOICEPRINT):
        emb = np.load(_LEGACY_VOICEPRINT)
        try:
            save_voiceprint(emb)  # migrate forward; keep the legacy copy
        except OSError:
            pass
        return emb
    return None


def save_voiceprint(emb: np.ndarray, path: str = VOICEPRINT):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.save(path, emb)
    try:
        os.chmod(path, 0o600)  # biometric — owner-only, same as secrets
    except OSError:
        pass
