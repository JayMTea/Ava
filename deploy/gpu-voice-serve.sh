#!/usr/bin/env bash
# gpu-voice-serve.sh — OPTIONAL GPU voice sidecar: natural TTS (Kokoro) + fast STT
# (Whisper), both on the GPU. The main bridge runs voice on CPU by default (Piper
# + faster-whisper); this sidecar upgrades quality AND speed when you opt in, and
# the bridge falls back to CPU automatically whenever it's not running.
#
# ── Why a sidecar ─────────────────────────────────────────────────────────────
# TTS/STT here need CUDA PyTorch, which the bridge venv deliberately does not
# carry (it stays lean/CPU). So this loads the models once in their own venv
# (.venv-tts) and serves them over localhost. Mirrors deploy/local-serve.sh.
#
# ── One-time / after reboot ───────────────────────────────────────────────────
#   bash deploy/gpu-voice-serve.sh              # creates .venv-tts, installs, runs
#
# Then enable the GPU path in the bridge env (defaults are CPU/Piper if unset):
#   AVA_TTS=kokoro          # natural GPU TTS   (else Piper)
#   AVA_STT=gpu             # fast GPU Whisper  (else CPU faster-whisper)
#   AVA_KOKORO_URL=http://127.0.0.1:8129
#   AVA_STT_URL=http://127.0.0.1:8129
#
# For an always-on setup, run this from a process manager (systemd user unit,
# supervisor, `docker compose`, …) — anything that keeps the port alive. It has
# no hardcoded personal paths; everything resolves from the repo + env.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/.env" ] && set -a && . "$REPO/.env" && set +a
cd "$REPO"

VENV="${AVA_TTS_VENV:-$REPO/.venv-tts}"
PORT="${AVA_VOICE_PORT:-8129}"
PYBASE="${AVA_TTS_PYTHON:-python3}"

# 1. Build the sidecar venv if missing. --system-site-packages lets it inherit a
#    CUDA torch already installed on the host (as on DGX/GB10 boxes). If your host
#    has no system torch, install a CUDA torch into $VENV yourself first.
if [ ! -x "$VENV/bin/python" ]; then
  echo "[gpu-voice] creating sidecar venv at $VENV (inheriting system packages)…"
  "$PYBASE" -m venv --system-site-packages "$VENV"
fi

# 2. Verify CUDA torch is importable; warn (don't fail) so CPU-only hosts can
#    still run it slowly for testing.
if ! "$VENV/bin/python" - <<'PY'
import sys
try:
    import torch
    print(f"[gpu-voice] torch {torch.__version__} cuda={torch.cuda.is_available()}")
    sys.exit(0 if torch.cuda.is_available() else 3)
except Exception as e:
    print(f"[gpu-voice] no usable torch: {e}")
    sys.exit(4)
PY
then
  echo "[gpu-voice] WARNING: CUDA torch not available — the sidecar will be SLOW"
  echo "[gpu-voice] on CPU. Install a CUDA torch into $VENV, e.g.:"
  echo "    $VENV/bin/pip install torch --index-url https://download.pytorch.org/whl/cu124"
fi

# 3. Install the sidecar deps (idempotent).
echo "[gpu-voice] installing requirements-voice-gpu.txt…"
"$VENV/bin/pip" install -q -r "$REPO/requirements-voice-gpu.txt"

# 4. Serve. Model load + first synth (~10-25s) happens at startup; the bridge
#    falls back to CPU during that window.
echo "[gpu-voice] serving Kokoro TTS + Whisper STT on 127.0.0.1:$PORT …"
exec "$VENV/bin/python" -m uvicorn kokoro_tts:app --host 127.0.0.1 --port "$PORT"
