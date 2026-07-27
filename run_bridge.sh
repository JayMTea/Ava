#!/usr/bin/env bash
# Start Ava's phone voice bridge and expose it to your phone over Tailscale.
#
#   ./run_bridge.sh
#
# Then open on your phone (Tailscale ON) at the URL printed on start:
#   https://<your-tailnet-host>:8445/
set -euo pipefail
cd "$(dirname "$0")"

# Load local secrets/overrides if present (gitignored). See .env.example.
[ -f .env ] && set -a && . ./.env && set +a

# Port: env APP_PORT/AVA_PORT wins, else ava.yaml server.port, else 8096.
APP_PORT="${APP_PORT:-${AVA_PORT:-$(./.venv/bin/python -c 'from ava_bridge import config; print(config.SERVER_PORT)' 2>/dev/null || echo 8096)}}"
export AVA_PORT="${APP_PORT}"
TS_PORT="${TS_PORT:-8445}"       # public Tailscale HTTPS port
# Public host for the phone URL: env TS_HOST wins, else this node's tailnet DNS
# name, else the local hostname. Nothing personal is baked in.
if [ -z "${TS_HOST:-}" ]; then
  TS_HOST="$(tailscale status --json 2>/dev/null \
    | grep -oE '"DNSName":[[:space:]]*"[^"]+"' | head -1 \
    | sed -E 's/.*"([^"]+)\."/\1/')"
fi
TS_HOST="${TS_HOST:-$(hostname -f 2>/dev/null || hostname)}"

# Ava backend (local open-model 30B on :8002) and TTS voice — same as the desktop loop.
export AVA_URL="${AVA_URL:-http://localhost:8002/v1/chat/completions}"
export AVA_MODEL="${AVA_MODEL:-Qwen/Qwen2.5-7B-Instruct}"
export WHISPER_MODEL="${WHISPER_MODEL:-small.en}"
export VOICE="${VOICE:-$PWD/models/en_US-amy-medium.onnx}"

# Phone-mic speaker gate (your voice only). 0 disables. Phone mics differ from
# the PC enrollment, so this is a touch more lenient than the USB-mic loop.
export AVA_PHONE_THRESHOLD="${AVA_PHONE_THRESHOLD:-0.40}"

# Publish over Tailscale (TLS + tailnet-only). Idempotent; needs sudo on most nodes.
if ! tailscale serve status 2>/dev/null | grep -q ":${TS_PORT}"; then
  echo "[bridge] adding Tailscale serve :${TS_PORT} -> 127.0.0.1:${APP_PORT}"
  sudo tailscale serve --bg --https="${TS_PORT}" "http://127.0.0.1:${APP_PORT}" \
    || echo "[bridge] WARN: could not set tailscale serve (run it manually with sudo)."
fi

echo "[bridge] phone URL:  https://${TS_HOST}:${TS_PORT}/"
exec ./.venv/bin/python serve.py
