#!/usr/bin/env bash
# Launch the Ava voice loop. Edit the device vars below after you plug in the mic.
set -euo pipefail
cd "$(dirname "$0")"

# Load local secrets/overrides if present (gitignored). See .env.example.
[ -f .env ] && set -a && . ./.env && set +a

# ---- Audio devices ---------------------------------------------------------
# Find these with:  ./devices.sh
# MIC_DEVICE:  arecord device for your USB mic   (e.g. plughw:1,0)
# OUT_DEVICE:  aplay device for the TV over HDMI (e.g. plughw:0,3 = HDMI 0)
export MIC_DEVICE="${MIC_DEVICE:-default}"
export OUT_DEVICE="${OUT_DEVICE:-}"     # empty = ALSA default; set to plughw:0,3 for TV HDMI
export PLAYER="${PLAYER:-aplay}"        # aplay (ALSA) | pw-play (PipeWire desktop session)

# ---- Ava backend (local open-model 30B on port 8002) ---------------------------------
export AVA_URL="${AVA_URL:-http://localhost:8002/v1/chat/completions}"
export AVA_MODEL="${AVA_MODEL:-nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8}"
export WHISPER_MODEL="${WHISPER_MODEL:-small.en}"

# Optional wake word gate (only respond when the word is heard). Empty = respond to any speech.
export WAKE_WORD="${WAKE_WORD:-}"

# Speaker verification: only respond to the enrolled voice (models/voiceprint.npy).
# Tuned from enrollment (mean consistency 0.83). 0 = disable the voice gate.
export SPEAKER_THRESHOLD="${SPEAKER_THRESHOLD:-0.55}"

# Pass through any args (e.g. --mode ptt). Defaults to hands-free listen mode.
exec ./.venv/bin/python voice_ava.py "$@"
