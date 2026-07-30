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


def load_voiceprint(path: str = ""):
    """Load the enrolled voiceprint; migrates a pre-existing legacy repo-local
    voiceprint (models/ under the CODE dir) into the persistent store so a
    Docker rebuild or AVA_HOME move never silently loses the enrollment."""
    # Resolved at CALL time, not bound as a default. `path: str = VOICEPRINT`
    # froze the module constant at import, so anything that reconfigured AVA_HOME
    # afterwards (a test, a simulator, a fleet instance) got the original path
    # here while `voiceprint_paths()` reported the new one — the loader and the
    # deleter could disagree about where the biometric lives, which is the worst
    # possible pair of functions to have out of step.
    path = path or VOICEPRINT
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


def save_voiceprint(emb: np.ndarray, path: str = ""):
    path = path or VOICEPRINT          # call-time, per load_voiceprint's note
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.save(path, emb)
    try:
        os.chmod(path, 0o600)  # biometric — owner-only, same as secrets
    except OSError:
        pass


def voiceprint_paths() -> list[str]:
    """Every path a voiceprint can live at, in the order `load_voiceprint` reads.

    There are two, and that is the whole reason deletion needs a function rather
    than an `os.remove`: `load_voiceprint` MIGRATES the legacy repo-local copy
    forward when the live one is absent (and its comment says "keep the legacy
    copy"). So removing only `VOICEPRINT` means the next gate check silently
    re-creates it — an eraser that does not erase.

    On a default bare-metal install `AVA_HOME` is the code root, so both entries
    collapse to one path and the bug is invisible. They diverge under Docker,
    which is the primary documented install — meaning a test written on the
    maintainer's own box would not have caught it.
    """
    seen: list[str] = []
    for p in (VOICEPRINT, _LEGACY_VOICEPRINT):
        if p not in seen:
            seen.append(p)
    return seen


def voiceprint_digest(path: str = "") -> str:
    """sha256 of a stored voiceprint's bytes, or "" if absent.

    Recorded BEFORE deletion so the ledger can say "an artifact with digest X
    existed and was destroyed at T". A 16-byte prefix of a hash over a 896-byte
    float vector is not reversible into a voiceprint, so destruction becomes
    provable without retaining the thing destroyed. Same move as
    `memory_store._digest`.
    """
    import hashlib
    for p in ([path] if path else voiceprint_paths()):
        try:
            with open(p, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            continue
    return ""


def delete_voiceprint() -> dict:
    """Destroy every stored voiceprint. Returns `{removed, absent, failed}`.

    Lives here rather than in `voice_enroll` or a route because THIS module owns
    both paths and the migration between them — a delete implemented anywhere else
    is a delete that does not know what it has to defeat.

    Deliberately does NOT touch `models/ecapa/**`: those are the public pretrained
    speaker-embedding weights, identical for every install, containing nothing
    about the owner. Removing them would cost an 80 MB re-download and destroy no
    personal data. The caller says so in the receipt rather than leaving it to
    inference.
    """
    removed: list[str] = []
    absent: list[str] = []
    failed: list[dict] = []
    for p in voiceprint_paths():
        if not os.path.exists(p):
            absent.append(p)
            continue
        try:
            os.remove(p)
            removed.append(p)
        except OSError as e:
            failed.append({"path": p, "error": str(e)})
    return {"removed": removed, "absent": absent, "failed": failed}
