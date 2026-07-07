"""Shared mutable runtime state + locks — the single source of truth.

Every module imports these objects (never re-binds them) so the whole app shares
one set of dicts/locks. `chat_store` populates `chats` from disk on import.
"""
import threading
import json
import os
from pathlib import Path

# Uploaded-attachment records: id -> {filename, kind, url, chars, ocr, text}.
attachments: dict[str, dict] = {}
attachments_lock = threading.Lock()

# Conversations: id -> {id, title, created, updated, messages[]}.
chats: dict[str, dict] = {}
chats_lock = threading.RLock()

# Async image/upscale jobs: id -> {id, kind, status, progress, url, ...}.
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# Live chain-of-thought turns: id -> {id, status, steps[], reply, job, error}.
turns: dict[str, dict] = {}
turns_lock = threading.Lock()

# Code-mode turns: id -> {id, status, steps[], reply, edits[], applied, error}.
# Ava edits her own source via Claude; edits are STAGED here until approved.
code_turns: dict[str, dict] = {}
code_turns_lock = threading.Lock()

# Recursive learning state: SEPARATE contexts for code-mode and general chat.
# code_learning_state: {last_cycle, cycles[], inline_fixes[]} — tracks code edits, commits, proposals
# chat_learning_state: {last_cycle, cycles[], inline_fixes[]} — tracks conversation patterns, topics
# Each has their own proposal approval workflow (gated).
code_learning_state: dict = {
    "last_cycle": None,
    "cycles": [],
    "inline_fixes": []
}
code_learning_state_lock = threading.Lock()

chat_learning_state: dict = {
    "last_cycle": None,
    "cycles": [],
    "inline_fixes": []
}
chat_learning_state_lock = threading.Lock()

# Serialises writes to logs/learning_state.json. Kept SEPARATE from the two
# state locks above so save_learning_state() can be safely called by handlers
# that already hold code_learning_state_lock or chat_learning_state_lock
# (threading.Lock is non-reentrant — re-acquiring it here would deadlock).
_persist_lock = threading.Lock()

# Per-IP login failure tracker for the brute-force throttle.
login_fails: dict[str, list] = {}
login_lock = threading.Lock()

# Lazily-initialised heavy objects (whisper STT, speaker verifier, voiceprint).
# `voice_unavailable` flips True when the optional voice deps (see
# requirements-voice.txt) aren't installed — the app runs voice-less.
heavy = {"whisper": None, "verifier": None, "voiceprint": None,
         "voice_unavailable": False}


# ===== Persistence for Learning State ========================================
def _ensure_logs_dir():
    """Ensure logs/ directory exists."""
    Path("logs").mkdir(exist_ok=True)


def save_learning_state():
    """Persist learning state to disk (called on updates + on shutdown).

    Uses a dedicated _persist_lock (NOT the two state locks) so it can be
    called from handlers that already hold code_learning_state_lock or
    chat_learning_state_lock without deadlocking.
    """
    try:
        _ensure_logs_dir()
        with _persist_lock:
            data = {
                "code": code_learning_state,
                "chat": chat_learning_state
            }
            with open("logs/learning_state.json", "w") as f:
                json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️  Warning: Could not save learning state: {e}")


def load_learning_state():
    """Load learning state from disk (called on startup)."""
    try:
        if not os.path.exists("logs/learning_state.json"):
            return
        with open("logs/learning_state.json", "r") as f:
            data = json.load(f)
        with code_learning_state_lock, chat_learning_state_lock:
            if "code" in data:
                code_learning_state.update(data["code"])
            if "chat" in data:
                chat_learning_state.update(data["chat"])
        print("✅ Loaded learning state from disk")
    except Exception as e:
        print(f"⚠️  Warning: Could not load learning state: {e}")


# Load learning state on startup
load_learning_state()
