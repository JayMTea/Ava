#!/usr/bin/env bash
# Ava one-line installer (self-hosted, plug-and-play).
#
# Easiest: clone your fork and run this from inside it —
#   git clone https://github.com/<you>/ava && cd ava/deploy && ./install.sh
# Or standalone (set AVA_REPO to your fork's git URL):
#   AVA_REPO=https://github.com/<you>/ava.git curl -fsSL <this-url> | bash
#
# What it does: detect hardware -> pick a profile -> write deploy/.env from
# deploy/profiles/<profile>.env -> resolve the model's vLLM flags -> build and
# start -> WAIT for the published port to answer before claiming success.
#
# Env knobs:
#   AVA_DIR       where to install         (default: ~/ava, ignored inside a clone)
#   AVA_PROFILE   cpu|gpu|cloud|full|agent (default: auto-detected)
#   AVA_MODEL     override the model the profile ships with
#   AVA_REPO      git URL to clone from    (only when NOT run inside a clone)
#   AVA_INSTALL_DRY_RUN=1   write .env and stop, touching no images (for CI)
#   AVA_FORCE_DOCKER=1      use Docker on an Apple-Silicon Mac anyway
#   AVA_SKIP_GPU_RUNTIME_CHECK=1  skip the nvidia-container-toolkit probe
set -euo pipefail

AVA_DIR="${AVA_DIR:-$HOME/ava}"
AVA_REPO="${AVA_REPO:-}"
_PROFILES="cpu gpu cloud full agent"

# If we're already inside a clone (the common `git clone && cd deploy && ./install.sh`
# path), install in place and skip cloning entirely.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$_SCRIPT_DIR" ] && git -C "$_SCRIPT_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
  AVA_DIR="$(git -C "$_SCRIPT_DIR" rev-parse --show-toplevel)"
  _IN_CLONE=1
fi

say() { printf '\033[34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Apple Silicon: Docker Desktop on macOS can't pass the Apple GPU through, so the
# Docker path runs inference CPU-only — slow, and it wastes a Mac's unified memory
# (the whole point of a Mac Studio). Steer to the Metal-accelerated bare-metal path
# unless the user explicitly opts into Docker with AVA_FORCE_DOCKER=1.
#
# Gated on arm64 as well as Darwin: an Intel Mac has no Metal unified-memory story
# worth steering to, and sending it down the bare-metal path just denies it the
# working Docker one.
if [ "$(uname -s 2>/dev/null || true)" = "Darwin" ] \
   && [ "$(uname -m 2>/dev/null || true)" = "arm64" ]; then
  say "Detected Apple Silicon ($(uname -m 2>/dev/null || echo '?'))."
  if [ "${AVA_FORCE_DOCKER:-0}" != "1" ]; then
    cat <<EOF
On Apple Silicon, run Ava bare-metal so inference uses the Metal GPU + unified
memory (Docker Desktop can't pass the GPU through, so its inference is CPU-only):

  cd "${AVA_DIR}"
  python3 -m venv .venv && . .venv/bin/activate
  pip install -r requirements.txt && pip install -e .
  ava setup && ava doctor && ava up

Then install a native engine and pull a model (do NOT use the vLLM default):
  brew install ollama && ollama serve
  ollama pull llama3.1:8b         # size it to your Mac; see docs/CHOOSE_A_MODEL.md
Full recipe: deploy/README.md, "Apple Silicon (Mac mini / Studio)".

To use the (CPU-only) Docker path anyway, re-run with AVA_FORCE_DOCKER=1.
EOF
    exit 0
  fi
  say "AVA_FORCE_DOCKER=1 — proceeding with the CPU-only Docker path on macOS."
fi

command -v docker >/dev/null 2>&1 || die "Docker is required — https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."

# A user-supplied profile must name a file we actually ship. Unvalidated, a typo
# was passed straight to compose, which started only the always-on `ava` service
# and no engine at all — an install that looks like it worked and cannot chat.
if [ -n "${AVA_PROFILE:-}" ]; then
  case " $_PROFILES " in
    *" $AVA_PROFILE "*) : ;;
    *) die "Unknown AVA_PROFILE '$AVA_PROFILE' — pick one of: $_PROFILES" ;;
  esac
fi

# Auto-pick a profile from available hardware.
# Having a GPU is not the same as having enough of one: the gpu profile serves a
# model on vLLM, and if the weights plus KV cache do not fit, the container fails
# its start check and Docker retries it. Detecting that here — where we can say so
# in one sentence — beats letting the user discover it from a restart loop.
if [ -z "${AVA_PROFILE:-}" ]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    AVA_PROFILE="gpu"
    _vram_mib="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
                 | tr -d ' ' | sort -rn | head -1)"
    case "$_vram_mib" in
      ''|*[!0-9]*) warn "Could not read GPU memory from nvidia-smi — assuming the gpu profile fits." ;;
      *)
        say "Detected GPU memory: ${_vram_mib} MiB"
        # The default model (see deploy/default-model.env) is ~15 GB of weights;
        # at the default 0.90 utilization it wants a ~16 GB card. Below that, CPU
        # is the honest choice — Ollama on CPU is slow but it actually starts.
        if [ "$_vram_mib" -lt 16000 ]; then
          warn "Only ${_vram_mib} MiB of GPU memory — the gpu profile's default model needs ~16 GB."
          warn "Falling back to the cpu profile. To use the GPU anyway, pick a smaller model:"
          warn "  AVA_PROFILE=gpu AVA_MODEL=Qwen/Qwen2.5-3B-Instruct ./install.sh"
          AVA_PROFILE="cpu"
        fi
        ;;
    esac

    # A driver is not a runtime. nvidia-smi proves the host can talk to the GPU;
    # it proves nothing about `docker run --gpus all` working, which needs the
    # NVIDIA container toolkit. Without it the vllm container dies at start with
    # a message about an unknown runtime, which reads like an Ava bug.
    if [ "$AVA_PROFILE" = "gpu" ] && [ "${AVA_SKIP_GPU_RUNTIME_CHECK:-0}" != "1" ]; then
      if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
        warn "nvidia-smi works, but Docker has no 'nvidia' runtime registered."
        warn "That is the NVIDIA Container Toolkit, installed separately from the driver:"
        warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
        warn "Falling back to the cpu profile. Re-run with AVA_SKIP_GPU_RUNTIME_CHECK=1 to override."
        AVA_PROFILE="cpu"
      fi
    fi
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

# ── deploy/.env ───────────────────────────────────────────────────────────────
# Everything this script decides goes in a MANAGED BLOCK, so re-running is
# idempotent and anything the user added by hand (an API key, a password) is
# preserved. The file is never truncated; it is only ever appended to or had its
# managed block replaced, and any pre-existing file is backed up first.
_ENV=".env"
_BEGIN="# >>> ava managed (deploy/install.sh) >>>"
_END="# <<< ava managed (deploy/install.sh) <<<"
_SRC="profiles/${AVA_PROFILE}.env"
[ -f "$_SRC" ] || die "No profile file at deploy/$_SRC — expected one of: $_PROFILES"

if [ -f "$_ENV" ]; then
  _bak=".env.bak.$(date +%Y%m%d-%H%M%S)"
  cp -p "$_ENV" "$_bak"
  say "Existing deploy/.env backed up to deploy/$_bak"
  # Drop only OUR block; leave every hand-written line alone.
  awk -v b="$_BEGIN" -v e="$_END" '
    $0 == b { skip = 1 } !skip { print } $0 == e { skip = 0 }
  ' "$_bak" > "$_ENV"
else
  : > "$_ENV"
  chmod 600 "$_ENV"
fi

# Resolve the model's vLLM flags from the one table (deploy/model-flags.conf).
# A tool-call parser that does not match the model returns NO tool calls, with no
# error anywhere — so this is not an optimisation, it is the difference between
# a working agent and turns that silently run to timeout.
_model="${AVA_MODEL:-$(grep -E '^AVA_MODEL=' "$_SRC" | head -1 | cut -d= -f2-)}"
{
  printf '%s\n' "$_BEGIN"
  printf '# profile: %s · written %s · re-run deploy/install.sh to refresh\n' \
         "$AVA_PROFILE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # The profile's own settings, minus any AVA_MODEL we are about to override.
  grep -v -E '^\s*#|^\s*$' "$_SRC" | grep -v -E '^AVA_MODEL=' || true
  [ -n "$_model" ] && printf 'AVA_MODEL=%s\n' "$_model"
  if [ "${AVA_PROFILE}" = "gpu" ] || [ "${AVA_PROFILE}" = "full" ] || [ "${AVA_PROFILE}" = "agent" ]; then
    bash ./resolve-model-flags.sh --env "$_model" 2>/dev/null || true
  fi
  printf '%s\n' "$_END"
} >> "$_ENV"
say "Wrote deploy/.env (profile: $AVA_PROFILE, model: ${_model:-<yours to set>})"

# Fail on a bad .env in one second, rather than after an image build.
if ! docker compose config >/dev/null 2>&1; then
  printf '\n'
  docker compose config 2>&1 | head -5
  printf '\n'
  die "deploy/.env does not produce a valid configuration (see above). For the cloud profile, fill in the empty values in deploy/.env."
fi

if [ "${AVA_INSTALL_DRY_RUN:-0}" = "1" ]; then
  say "AVA_INSTALL_DRY_RUN=1 — wrote deploy/.env and validated it; not building."
  exit 0
fi

say "Building & starting Ava (profile: $AVA_PROFILE) — first run downloads images/models"
docker compose up -d --build

# Ollama's model store starts empty and nothing else fills it, so a cpu install
# that stopped here would present a chat box wired to a server with no model.
if [ "$AVA_PROFILE" = "cpu" ]; then
  _ollama_model="$(grep -E '^AVA_MODEL=' "$_ENV" | tail -1 | cut -d= -f2-)"
  if [ -n "$_ollama_model" ]; then
    say "Pulling ${_ollama_model} into Ollama (first run only; this takes a while)"
    docker compose exec -T ollama ollama pull "$_ollama_model" \
      || warn "Could not pull ${_ollama_model} — run: docker compose exec ollama ollama pull ${_ollama_model}"
  fi
fi

# Wait for the PUBLISHED port, from the host. The container's own healthcheck
# curls its in-namespace loopback, so it reports healthy even when the published
# port has nothing behind it — that exact combination shipped once. Only an
# external probe proves a browser can reach it.
say "Waiting for http://localhost:8096 ..."
_ok=0
for _ in $(seq 1 90); do
  if curl -fsS -o /dev/null --max-time 3 http://localhost:8096/api/health 2>/dev/null; then
    _ok=1; break
  fi
  sleep 2
done

if [ "$_ok" = 1 ]; then
  say "Ava is up. Open http://localhost:8096 — the first screen lets you set your admin password."
else
  warn "Ava did not answer on http://localhost:8096 within ~3 minutes."
  warn "It may still be pulling a model. Check with:"
  warn "  cd $AVA_DIR/deploy && docker compose logs -f ava"
  warn "  cd $AVA_DIR/deploy && docker compose ps"
fi
say "Logs:  cd $AVA_DIR/deploy && docker compose logs -f ava"
