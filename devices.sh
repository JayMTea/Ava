#!/usr/bin/env bash
# List audio devices so you can pick MIC_DEVICE (capture) and OUT_DEVICE (playback).
#
# A thin wrapper on purpose. This script used to reimplement device enumeration in
# platform-specific shell (`aplay -l` / `arecord -l`), which meant it printed
# "no capture device found — plug in your USB mic" on a Mac with a perfectly good
# built-in microphone. One implementation now lives in ava_bridge/audio_io.py and
# both this and the voice loop read it, so they cannot disagree about your hardware.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]:-$0}")"

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required to enumerate audio devices." >&2
  exit 1
}

python3 -m ava_bridge.audio_io --list

cat <<'EOF'

Set them for a run, e.g.:
  Linux : MIC_DEVICE=plughw:1,0 OUT_DEVICE=plughw:0,3 ./run.sh
  macOS : MIC_DEVICE=:1 ./run.sh          # the index from the list above
EOF
