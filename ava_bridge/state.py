"""Shared mutable runtime state + locks — the single source of truth.

Every module imports these objects (never re-binds them) so the whole app shares
one set of dicts/locks. `chat_store` populates `chats` from disk on import.
"""
import threading

# Uploaded-attachment records: id -> {filename, kind, url, chars, ocr, text}.
attachments: dict[str, dict] = {}
attachments_lock = threading.Lock()

# Conversations: id -> {id, title, created, updated, messages[]}.
chats: dict[str, dict] = {}
chats_lock = threading.RLock()

# Live chain-of-thought turns: id -> {id, status, steps[], reply, error}.
turns: dict[str, dict] = {}
turns_lock = threading.Lock()

# Per-IP login failure tracker for the brute-force throttle.
login_fails: dict[str, list] = {}
login_lock = threading.Lock()

# Timestamp of the last interactive user turn (start or finish). Used to
# distinguish generations the user drove from background/idle token burn (the
# "agent kept spending while you were away" signal). 0 = never.
interaction = {"ts": 0.0}

# Lazily-initialised heavy objects (whisper STT, speaker verifier, voiceprint).
# `voice_unavailable` flips True when the optional voice deps (see
# requirements-voice.txt) aren't installed — the app runs voice-less.
heavy = {"whisper": None, "verifier": None, "voiceprint": None,
         "voice_unavailable": False}

# Serialises every mutation of `heavy`, and every read that is followed by a
# dereference. It is the only dict in this module that lacked one, which was
# survivable while the only writer was `_ensure_loaded()` at startup. Two things
# changed that: a voice turn re-reads these three values across a window that spans
# an ffmpeg decode and a sidecar round-trip, and there is now a release lever that
# can null them from a background thread while that window is open. Reentrant
# because `release_voice_models` calls `clear_voice_gate`, which takes it too.
heavy_lock = threading.RLock()


def clear_voice_gate() -> dict:
    """Evict the cached voiceprint AND verifier together. Returns what was held.

    Both, never one. `phone_bridge` guards the voice gate on `verifier is not
    None` and then dereferences `voiceprint` on the next line:

        if state.heavy["verifier"] is not None:
            sim = spk.cosine(state.heavy["voiceprint"], ...)

    so clearing the print alone turns every voice turn into a `TypeError` instead
    of the documented fail-open. Clearing both makes `_ensure_loaded()` re-read
    from disk on the next turn, find nothing, and leave the gate off — which is
    the behaviour `hub/voice.py` already documents for an un-enrolled box.

    Under `heavy_lock`, because "both, never one" is a claim about what a CONCURRENT
    reader can observe, and two bare assignments do not make it: a turn scheduled
    between them sees exactly the half-cleared state this exists to prevent.

    Lives in `state` rather than in `hub/voice.py` because a hub route importing
    `phone_bridge` would be circular.
    """
    with heavy_lock:
        held = {"voiceprint": heavy.get("voiceprint") is not None,
                "verifier": heavy.get("verifier") is not None}
        heavy["voiceprint"] = None
        heavy["verifier"] = None
        return held


def voice_snapshot() -> tuple:
    """`(whisper, verifier, voiceprint)` read atomically, for one turn to use.

    A voice turn must bind these ONCE and then use what it bound. Reading
    `heavy["whisper"]` at the point of use instead means a release landing mid-turn
    turns into `AttributeError: 'NoneType' has no attribute 'transcribe'` — an
    unhandled 500 — rather than the lazy reload the design intends. Holding the
    objects also keeps them alive for the duration, so a release during a turn frees
    nothing until the turn ends, which is the honest outcome and the one the
    allocator's measured-freed contract will correctly report.
    """
    with heavy_lock:
        return (heavy.get("whisper"), heavy.get("verifier"), heavy.get("voiceprint"))


def release_voice_models() -> dict:
    """Drop the bridge's in-process voice models. Returns what was held.

    They reload on the next voice turn: `phone_bridge._ensure_loaded()` runs per
    request and rebuilds whatever is None, so this needs no restore lever of its own.

    Deliberately does NOT touch `voice_unavailable`. That flag is sticky and one-way
    — it means "the optional deps are not installed" — and setting it here would
    disable voice permanently with no path back.

    Honest about scale: this is a few hundred MB of HOST RAM (faster-whisper int8 on
    CPU, plus an ECAPA verifier), not accelerator memory. On a box whose pool reading
    is VRAM it will not move the number at all. It is here for hygiene and for
    biometric residency, not capacity — see docs/ALLOCATION.md.
    """
    with heavy_lock:
        held = {"whisper": heavy.get("whisper") is not None}
        heavy["whisper"] = None
        held.update(clear_voice_gate())
    # The enrolment path keeps its OWN ECAPA singleton, separate from
    # heavy["verifier"] — a box that has ever run enrolment holds two. Releasing one
    # and calling it done would leave half the memory behind.
    try:
        from . import voice_enroll
        held["enroll_verifier"] = voice_enroll.release_verifier()
    except Exception:  # noqa: BLE001 — optional path; never fail a release for it
        held["enroll_verifier"] = False
    return held


