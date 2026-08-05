"""Voiceprint enrollment from uploaded audio — the browser path.

Ports the core of enroll_from_file.py behind functions the Hub API can call:
decode any container (webm/opus from MediaRecorder, wav, m4a, …) via ffmpeg,
window the speech, embed with ECAPA (speaker.py), reject outliers, and save the
averaged voiceprint to speaker.VOICEPRINT — the same file the /api/talk gate
reads. Heavy deps (torch/speechbrain via speaker.SpeakerVerifier) are loaded
lazily and everything degrades to a clear error string when voice extras or
ffmpeg aren't installed, so the Hub can always show WHY enrollment is
unavailable instead of a 500.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

RATE = 16000
WIN_S = 4.0     # window length per embedding
HOP_S = 2.0     # hop between windows (overlap)
MIN_RMS = 0.01  # drop near-silent windows
TRIM_STD = 1.0  # outlier rejection: drop windows > N std-devs below mean

_verifier = None
_verifier_lock = threading.Lock()


def deps() -> dict:
    """What's installed — lets the UI explain exactly what's missing."""
    out = {"ffmpeg": bool(shutil.which("ffmpeg")), "voice": False, "error": ""}
    try:
        import numpy  # noqa: F401
        import speechbrain  # noqa: F401
        import torch  # noqa: F401
        out["voice"] = True
    except ImportError as e:
        out["error"] = f"voice extras not installed ({e.name}) — pip install -r requirements-voice.txt"
    if not out["ffmpeg"]:
        out["error"] = (out["error"] + "; " if out["error"] else "") + "ffmpeg not found on PATH"
    return out


def _get_verifier():
    """Lazy singleton — the ECAPA model is ~80MB and loads once."""
    global _verifier
    with _verifier_lock:
        if _verifier is None:
            import speaker as spk
            _verifier = spk.SpeakerVerifier()
    return _verifier


def release_verifier() -> bool:
    """Drop this module's ECAPA model. True if one was held.

    This is a SECOND ECAPA singleton, separate from `state.heavy["verifier"]`: that
    one gates voice turns, this one serves enrolment and the "test my voice" button.
    A box that has ever run enrolment holds both, ~80 MB each, and neither
    `clear_voice_gate()` nor anything else touched this one — so a release that
    stopped at `state.heavy` would leave half the memory behind and report the whole
    of it as freed. `_get_verifier` rebuilds it on the next enrolment.
    """
    global _verifier
    with _verifier_lock:
        held = _verifier is not None
        _verifier = None
    return held


def _decode(data: bytes):
    """Any audio bytes -> 16k mono float32 via ffmpeg."""
    import numpy as np
    cp = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(RATE),
         "-f", "s16le", "pipe:1"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if cp.returncode != 0:
        raise ValueError("could not decode audio: "
                         + cp.stderr.decode(errors="ignore")[-200:])
    return np.frombuffer(cp.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _windows(audio):
    win, hop = int(WIN_S * RATE), int(HOP_S * RATE)
    if len(audio) <= win:
        yield audio
        return
    for start in range(0, len(audio) - win + 1, hop):
        yield audio[start:start + win]


def _embed_clips(clips: list[bytes]):
    """Decode + window + embed every clip. Returns (embeddings, total_seconds)."""
    import numpy as np
    verifier = _get_verifier()
    embs, total = [], 0.0
    for data in clips:
        audio = _decode(data)
        total += len(audio) / RATE
        for w in _windows(audio):
            if float(np.sqrt(np.mean(w * w))) < MIN_RMS:
                continue  # silence
            embs.append(verifier.embed_audio(np.ascontiguousarray(w)))
    return embs, total


def enroll(clips: list[bytes]) -> dict:
    """Build and SAVE the voiceprint from uploaded clips. Returns quality stats
    or {'ok': False, 'error': ...} — never raises for expected failures."""
    import numpy as np
    import speaker as spk
    d = deps()
    if not (d["voice"] and d["ffmpeg"]):
        return {"ok": False, "error": d["error"]}
    try:
        embs, total = _embed_clips(clips)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if len(embs) < 2:
        return {"ok": False,
                "error": "not enough speech captured — record at least ~10 seconds of talking"}

    embs = np.array(embs)
    embs = embs / np.linalg.norm(embs, axis=1, keepdims=True)

    def centroid(e):
        c = e.mean(axis=0)
        return c / np.linalg.norm(c)

    vp = centroid(embs)
    sims = np.array([spk.cosine(vp, e) for e in embs])
    dropped = 0
    if len(embs) >= 4:  # outlier rejection, then a tighter centroid
        mask = sims >= (sims.mean() - TRIM_STD * sims.std())
        if 2 <= mask.sum() < len(embs):
            dropped = int((~mask).sum())
            vp = centroid(embs[mask])
            sims = np.array([spk.cosine(vp, e) for e in embs[mask]])

    os.makedirs(os.path.dirname(spk.VOICEPRINT), exist_ok=True)
    spk.save_voiceprint(vp)
    # Enrollment was unaudited, which meant the ledger could not answer "when did
    # biometric data start existing on this box" — and you cannot evidence a
    # retention-and-destruction policy without a creation record to bound it.
    # The digest, not the vector: enough to tie this record to the later deletion
    # record, and not reversible into a voiceprint.
    from . import audit as _audit
    _audit.record("voiceprint", action="enroll",
                  seconds=round(total, 1), windows=int(len(embs)),
                  dropped=dropped,
                  consistency_mean=round(float(sims.mean()), 3),
                  digest=spk.voiceprint_digest())
    return {
        "ok": True,
        "seconds": round(total, 1),
        "windows": int(len(embs)),
        "dropped": dropped,
        "consistency": {"min": round(float(sims.min()), 3),
                        "mean": round(float(sims.mean()), 3),
                        "max": round(float(sims.max()), 3)},
        "suggested_threshold": round(max(0.25, float(sims.min()) - 0.05), 2),
        "low_consistency": bool(sims.mean() < 0.6),
    }


def test(clip: bytes) -> dict:
    """Similarity of one clip against the enrolled voiceprint."""
    import numpy as np
    import speaker as spk
    d = deps()
    if not (d["voice"] and d["ffmpeg"]):
        return {"ok": False, "error": d["error"]}
    vp = spk.load_voiceprint()
    if vp is None:
        return {"ok": False, "error": "no voiceprint enrolled yet"}
    try:
        embs, _ = _embed_clips([clip])
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not embs:
        return {"ok": False, "error": "no speech detected in the clip"}
    embs = np.array(embs)
    embs = embs / np.linalg.norm(embs, axis=1, keepdims=True)
    sims = [spk.cosine(vp, e) for e in embs]
    return {"ok": True, "similarity": round(float(np.mean(sims)), 3),
            "windows": len(sims)}


def status() -> dict:
    import speaker as spk
    from . import config
    d = deps()
    return {
        "deps_ok": d["voice"] and d["ffmpeg"],
        "deps_error": d["error"],
        "enrolled": os.path.exists(spk.VOICEPRINT),
        "threshold": config.PHONE_THRESHOLD,
    }


# ---- destruction ------------------------------------------------------------ #
# BIPA §15(a) requires a WRITTEN retention-and-destruction policy for biometric
# identifiers, and GDPR Art. 9 makes a voiceprint special-category data. Before
# this there was no code path to delete one at all — not a route, not a function,
# nothing (a repo-wide grep for voiceprint ∩ delete|remove|forget returned zero).
# See docs/BIOMETRICS.md, which is the written half.
def delete(*, reset_threshold: bool = True) -> dict:
    """Destroy the enrolled voiceprint and everything derived from it.

    Returns a RECEIPT the owner can check by hand, listing absolute paths — the
    status endpoint saying "enrolled: false" is the tool's own report, which is
    the weakest of the three proofs available and the one this project's doctrine
    says not to rely on.

    The receipt also names what was deliberately NOT destroyed. That is not
    padding: a receipt claiming more than it did is the failure mode here, and
    naming the two deliberate exclusions is what makes the rest of it credible.
    """
    import speaker as spk
    from . import audit, config, settings, state

    # Captured before anything is removed — a record written afterwards cannot say
    # what was there.
    digest = spk.voiceprint_digest()
    receipt: dict = {"ok": True, "digest_before": digest}

    # 1 + 2. Both stored copies, including the legacy one load_voiceprint()
    #        migrates forward. Removing only the live path resurrects the print.
    gone = spk.delete_voiceprint()
    receipt.update(gone)

    # 3 + 4. The running process's cached copy, both keys together.
    receipt["in_memory_evicted"] = state.clear_voice_gate()

    # 5. A print-derived threshold is residue: the UI would otherwise show a gate
    #    tuned to a voiceprint that no longer exists.
    if reset_threshold:
        try:
            before = config.PHONE_THRESHOLD
            settings.save_patch({"voice": {"threshold": None}})
            receipt["threshold_reset_from"] = before
        except Exception as e:  # noqa: BLE001 — the biometric is gone either way
            receipt["threshold_reset_error"] = str(e)

    # 6. Raw owner audio Ava itself wrote (only under AVA_DEBUG_TALK).
    last = os.path.join(settings.logs_dir(), "last_talk.wav")
    if os.path.isfile(last):
        try:
            os.remove(last)
            receipt["removed"].append(last)
        except OSError as e:
            receipt.setdefault("failed", []).append({"path": last, "error": str(e)})

    # 7 + 8. Named, not touched.
    enroll_dir = os.path.join(settings.CODE_ROOT, "enroll")
    kept = []
    try:
        kept = sorted(f for f in os.listdir(enroll_dir)
                      if f.lower().endswith((".wav", ".m4a", ".mp3")))
    except OSError:
        pass
    receipt["not_destroyed"] = {
        "models/ecapa/**": ("public pretrained speaker-embedding weights, "
                            "identical for every install — nothing about you"),
        "enroll/": (f"{len(kept)} source recording(s) YOU supplied to the CLI; "
                    "deleting your own files unasked would be overreach"
                    if kept else "no source recordings present"),
    }
    receipt["enroll_files_kept"] = kept

    audit.record("voiceprint", action="delete",
                 removed=receipt.get("removed", []),
                 absent=receipt.get("absent", []),
                 failed=receipt.get("failed", []),
                 digest_before=digest,
                 in_memory_evicted=receipt["in_memory_evicted"],
                 threshold_reset_from=receipt.get("threshold_reset_from"),
                 not_destroyed=sorted(receipt["not_destroyed"]))
    return receipt
