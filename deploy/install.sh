#!/usr/bin/env bash
# Ava one-line installer (self-hosted, plug-and-play).
#
# Easiest: clone your fork and run this from inside it —
#   git clone https://github.com/<you>/ava && cd ava/deploy && ./install.sh
# Or standalone (set AVA_REPO to your fork's git URL):
#   AVA_REPO=https://github.com/<you>/ava.git curl -fsSL <this-url> | bash
#
# Env knobs:
#   AVA_DIR      where to install        (default: ~/ava, ignored if run in a clone)
#   AVA_PROFILE  cpu | gpu | cloud | full (default: auto-detected)
#   AVA_REPO     git URL to clone from   (required only when NOT run inside a clone)
set -euo pipefail

AVA_DIR="${AVA_DIR:-$HOME/ava}"
AVA_REPO="${AVA_REPO:-}"

# If we're already inside a clone (the common `git clone && cd deploy && ./install.sh`
# path), install in place and skip cloning entirely.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$_SCRIPT_DIR" ] && git -C "$_SCRIPT_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
  AVA_DIR="$(git -C "$_SCRIPT_DIR" rev-parse --show-toplevel)"
  _IN_CLONE=1
fi

say() { printf '\033[34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Apple Silicon: Docker Desktop on macOS can't pass the Apple GPU through, so the
# Docker path runs inference CPU-only — slow, and it wastes a Mac's unified memory
# (the whole point of a Mac Studio). Steer to the Metal-accelerated bare-metal path
# unless the user explicitly opts into Docker with AVA_FORCE_DOCKER=1.
if [ "$(uname -s 2>/dev/null || true)" = "Darwin" ]; then
  say "Detected macOS ($(uname -m 2>/dev/null || echo '?'))."
  if [ "${AVA_FORCE_DOCKER:-0}" != "1" ]; then
    cat <<EOF
On Apple Silicon, run Ava bare-metal so inference uses the Metal GPU + unified
memory (Docker Desktop can't pass the GPU through, so its inference is CPU-only):

  cd "${AVA_DIR}"
  python3 -m venv .venv && . .venv/bin/activate
  pip install -r requirements.txt
  ./bin/ava setup && ./bin/ava doctor && ./bin/ava up

Then install a native engine and pull a model (do NOT use the vLLM default):
  brew install ollama && ollama serve
  ollama pull llama3.1:70b        # sized to your Mac; see config.example.yaml
Full recipe: deploy/README.md, "Apple Silicon (Mac mini / Studio)".

To use the (CPU-only) Docker path anyway, re-run with AVA_FORCE_DOCKER=1.
EOF
    exit 0
  fi
  say "AVA_FORCE_DOCKER=1 — proceeding with the CPU-only Docker path on macOS."
fi

command -v docker >/dev/null 2>&1 || die "Docker is required — https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."

# Auto-pick a profile from available hardware.
if [ -z "${AVA_PROFILE:-}" ]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    AVA_PROFILE="gpu"
  else
    AVA_PROFILE="cpu"
  fi
fi
say "Hardware profile: $AVA_PROFILE"

if [ "${_IN_CLONE:-0}" = "1" ]; then
  say "Installing in place from this clone: $AVA_DIR"
elif [ -d "$AVA_DIR/.git" ]; then
  say "Updating existing install in $AVA_DIR"
  git -C "$AVA_DIR" pull --ff-only || true
else
  [ -n "$AVA_REPO" ] || die "Set AVA_REPO to your fork's git URL, or run this from inside a clone (git clone <you>/ava && cd ava/deploy && ./install.sh)."
  say "Cloning Ava into $AVA_DIR"
  git clone --depth 1 "$AVA_REPO" "$AVA_DIR" || die "clone failed"
fi

cd "$AVA_DIR/deploy"
say "Building & starting Ava (profile: $AVA_PROFILE) — first run downloads images/models"
docker compose --profile "$AVA_PROFILE" up -d --build

say "Done. Open http://localhost:8096 — the first screen lets you set your admin password."
say "Logs:  cd $AVA_DIR/deploy && docker compose logs -f ava"
