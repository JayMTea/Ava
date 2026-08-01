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
#   AVA_PROFILE   cpu|gpu|rocm|cloud|full|agent (default: auto-detected)
#   AVA_MODEL     override the model the profile ships with
#   AVA_REPO      git URL to clone from    (only when NOT run inside a clone)
#   AVA_INSTALL_DRY_RUN=1   write .env and stop, touching no images (for CI)
#   AVA_FORCE_DOCKER=1      use Docker on an Apple-Silicon Mac anyway
#   AVA_SKIP_GPU_RUNTIME_CHECK=1  skip the nvidia-container-toolkit probe
#   AVA_NO_BROWSER=1        do not open the first-run link (it is still printed)
set -euo pipefail

AVA_DIR="${AVA_DIR:-$HOME/ava}"
AVA_REPO="${AVA_REPO:-}"
_PROFILES="cpu gpu rocm cloud full agent"

# If we're already inside a clone (the common `git clone && cd deploy && ./install.sh`
# path), install in place and skip cloning entirely.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$_SCRIPT_DIR" ] && git -C "$_SCRIPT_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
  AVA_DIR="$(git -C "$_SCRIPT_DIR" rev-parse --show-toplevel)"
  _IN_CLONE=1
fi

open_browser() {
  # Open the first-run link for the owner, the way `jupyter notebook` does — the
  # token is not a secret they are supposed to handle, it is a proof they own the
  # machine, and making them shuttle it by hand is ceremony, not security.
  #
  # Every skip below is a case where opening a browser is WRONG, not merely
  # unnecessary, so each is checked rather than attempted-and-ignored:
  #   * over SSH, the browser would open on the wrong machine (or not at all,
  #     after a timeout the installer would appear to hang on)
  #   * in CI there is no session to open into and xdg-open can block on a dbus
  #     call that never answers
  #   * headless Linux has no display, and xdg-open there prints an error that
  #     reads like an install failure
  # The link is ALWAYS printed regardless, so this can only ever save a step —
  # never be the only way through.
  [ "${AVA_NO_BROWSER:-0}" = "1" ] && return 0
  [ -n "${CI:-}" ] && return 0
  [ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-}" ] && return 0
  case "$(uname -s 2>/dev/null || true)" in
    Darwin) command -v open >/dev/null 2>&1 && open "$1" >/dev/null 2>&1 & ;;
    MINGW*|MSYS*|CYGWIN*)
      # Git Bash: `start` is a cmd builtin, not a program. The empty "" is the
      # window title cmd would otherwise take the URL for.
      command -v cmd >/dev/null 2>&1 && cmd //c start "" "$1" >/dev/null 2>&1 & ;;
    Linux)
      [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && return 0
      command -v xdg-open >/dev/null 2>&1 && xdg-open "$1" >/dev/null 2>&1 & ;;
    *) return 0 ;;
  esac
  return 0
}

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
_BARE_METAL=0
if [ "$(uname -s 2>/dev/null || true)" = "Darwin" ] \
   && [ "$(uname -m 2>/dev/null || true)" = "arm64" ]; then
  say "Detected Apple Silicon ($(uname -m 2>/dev/null || echo '?'))."
  if [ "${AVA_FORCE_DOCKER:-0}" != "1" ]; then
    # This used to print the recipe and `exit 0` — a documented platform with no
    # installer, which is why deploy/platforms.conf could not honestly call Apple
    # Silicon anything better than simulated. It now installs, further down, after
    # the clone step so there is a checkout to install INTO.
    _BARE_METAL=1
    say "Installing bare metal so inference can use the Metal GPU and unified memory."
    say "(Docker Desktop cannot pass the Apple GPU through; AVA_FORCE_DOCKER=1 forces it.)"
  else
    say "AVA_FORCE_DOCKER=1 — proceeding with the CPU-only Docker path on macOS."
  fi
fi

# Docker is a requirement of the Docker path only. Demanding it on the bare-metal
# path would refuse to install on exactly the machine that cannot use it.
if [ "$_BARE_METAL" = "0" ]; then
  command -v docker >/dev/null 2>&1 || die "Docker is required — https://docs.docker.com/get-docker/"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
fi

# A user-supplied profile must name a file we actually ship. Unvalidated, a typo
# was passed straight to compose, which started only the always-on `ava` service
# and no engine at all — an install that looks like it worked and cannot chat.
if [ -n "${AVA_PROFILE:-}" ]; then
  case " $_PROFILES " in
    *" $AVA_PROFILE "*) : ;;
    *) die "Unknown AVA_PROFILE '$AVA_PROFILE' — pick one of: $_PROFILES" ;;
  esac
fi

# Ask the HAL what this machine is. install.sh used to run its own vendor probes,
# which meant the installer's idea of the hardware and the app's could disagree —
# and did: `nvidia-smi`-or-CPU has no branch for ROCm, Level Zero or an APU, so a
# 128 GB Strix Halo box silently got the CPU-Ollama profile with no explanation.
# One detector, in ava_bridge/hwinfo.py, is the fix; deploy/platforms.conf maps
# its answer to a profile.
#
# This runs unconditionally, even when the user pinned AVA_PROFILE, because the
# platform line and its verification tier are worth saying either way — an owner
# should learn at install time whether their hardware class is verified on real
# hardware or merely simulated. Only the profile assignment is conditional.
#
# Best-effort on purpose: with AVA_REPO set this runs BEFORE the clone, so the
# module may not exist yet. Failure falls through to the shell probes below, which
# remain the only vendor detection allowed in this file.
if [ -n "${_SCRIPT_DIR:-}" ] && command -v python3 >/dev/null 2>&1; then
  _detect="$(cd "${_SCRIPT_DIR}/.." 2>/dev/null \
             && python3 -m ava_bridge.platforms --install-detect 2>/dev/null)" \
    || _detect=""
  if [ -n "${_detect}" ]; then
    eval "${_detect}"
    say "Platform: ${AVA_DETECTED_LABEL} [${AVA_DETECTED_TIER}]"
    case "${AVA_DETECTED_TIER}" in
      ci-simulated)
        warn "This hardware class is tested by simulation, not on real hardware."
        warn "The install should work; the numbers Ava reports are unconfirmed here."
        warn "Help fix that: python3 tools/ondevice_check.py --record" ;;
      unsupported)
        warn "${AVA_DETECTED_LABEL} is not a supported target — continuing anyway." ;;
    esac
    # An APU reports a tiny BIOS VRAM carve-out, so say which memory model was
    # chosen and how to override it. Getting this wrong is the difference between
    # gating on 128 GB and gating on 512 MiB.
    case "${AVA_DETECTED_MEM_MODEL}" in
      unified)  say "Memory model: unified (system RAM is the fit pool). Override: AVA_GPU_MEMORY_MODEL=discrete" ;;
      discrete) say "Memory model: discrete (GPU VRAM is the fit pool). Override: AVA_GPU_MEMORY_MODEL=unified" ;;
    esac
    [ -z "${AVA_PROFILE:-}" ] && AVA_PROFILE="${AVA_DETECTED_PROFILE}"
  fi
fi

# `bare-metal` is a real value in deploy/platforms.conf but NOT a compose profile,
# so it must never reach `profiles/<name>.env`. It gets here when a Mac user passes
# AVA_FORCE_DOCKER=1: the HAL still says darwin-apple, the table still says
# bare-metal, and the Docker path would then die looking for profiles/bare-metal.env
# with a message about an unknown profile — technically true, unhelpful, and the
# user's own flag caused it. CPU-only Docker is the honest answer there.
if [ "${AVA_PROFILE:-}" = "bare-metal" ]; then
  if [ "$_BARE_METAL" = "1" ]; then
    : # the bare-metal installer below handles it; nothing to map
  else
    say "Docker on this platform has no GPU access, so using the cpu profile."
    AVA_PROFILE="cpu"
  fi
fi

# HAL-EXEMPT-BEGIN: identification fallback — the HAL could not answer (no clone
# yet, or no python3), so a coarse shell probe is better than refusing to install.
# tests/test_install_detection.py bounds this region.
if [ -z "${AVA_PROFILE:-}" ]; then
  # shellcheck disable=SC2015
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    AVA_PROFILE="gpu"
  elif command -v rocm-smi >/dev/null 2>&1 && rocm-smi >/dev/null 2>&1; then
    AVA_PROFILE="rocm"
  else
    AVA_PROFILE="cpu"
  fi
  warn "Detected the profile with shell probes (the HAL was unavailable): ${AVA_PROFILE}"
fi
# HAL-EXEMPT-END

# HAL-EXEMPT-BEGIN: sizing, not identification. "Is there ENOUGH GPU for the
# default model" and "is a container runtime registered" are different questions
# from "what hardware is this", and both are properly the installer's business.
if [ "$AVA_PROFILE" = "gpu" ] && [ "${AVA_DETECTED_MEM_MODEL:-}" = "unified" ]; then
  # On unified memory the VRAM query returns N/A BY DESIGN, so the discrete-VRAM
  # sizing gate below does not apply and warning that it "could not read GPU
  # memory" is noise about a reading that cannot exist. Model fit on this class is
  # a system-RAM question, which model_fit.py already answers at runtime.
  say "Unified memory — skipping the discrete-VRAM sizing check (fit is a system-RAM question here)."
elif [ "$AVA_PROFILE" = "gpu" ]; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    _vram_mib="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
                 | tr -d ' ' | sort -rn | head -1)"
    case "$_vram_mib" in
      ''|*[!0-9]*) warn "Could not read GPU memory from nvidia-smi — assuming the gpu profile fits." ;;
      *)
        say "Detected GPU memory: ${_vram_mib} MiB"
        # ARITHMETIC, not a remembered number. The old gate was 16000 MiB, which
        # green-lit an RTX 4080 (16376 MiB) for a model that cannot serve on it:
        #
        #   Qwen2.5-7B BF16 weights        14.2 GiB
        #   usable at --gpu-memory-utilization 0.90 of 16376 MiB
        #                                  14.4 GiB
        #   -> the weights fit with 0.2 GiB spare, and a 32768-token KV cache
        #      needs ~2 GiB, so vLLM OOMs on load.
        #
        # And the context cannot simply be shortened: deploy/default-model.env
        # records that 32768 is chosen to clear the ~29k tokens Ava's own system
        # prompt and tool schemas occupy. A shorter context breaks the agent, so on
        # a card this size the honest lever is a SMALLER MODEL, not a smaller window.
        #
        #   weights + KV at 0.90  =>  (14.2 + 2) / 0.90  =  18.0 GiB
        #
        # Three tiers, so a 16 GB card gets a working GPU install instead of being
        # dumped to CPU:
        _need_default_mib=18000
        _need_small_mib=12000
        if [ "$_vram_mib" -lt "$_need_small_mib" ]; then
          warn "Only ${_vram_mib} MiB of GPU memory — too little to serve a useful"
          warn "model, so falling back to the cpu profile. Ollama on CPU is slow but"
          warn "it actually starts."
          AVA_PROFILE="cpu"
        elif [ "$_vram_mib" -lt "$_need_default_mib" ]; then
          # A 12-18 GB card: keep the GPU, downshift the model. 3B is ~6.2 GiB of
          # weights and resolves the same hermes tool parser and the same 32768
          # context in deploy/model-flags.conf, so nothing else changes.
          if [ -z "${AVA_MODEL:-}" ]; then
            AVA_MODEL="Qwen/Qwen2.5-3B-Instruct"
            warn "${_vram_mib} MiB of GPU memory: the default 7B needs ~${_need_default_mib} MiB"
            warn "(14.2 GiB of weights plus a 32k KV cache at 0.90 utilization)."
            warn "Serving ${AVA_MODEL} instead — same tool parser, same 32k context."
            warn "To force the 7B anyway: AVA_MODEL=Qwen/Qwen2.5-7B-Instruct ./install.sh"
            export AVA_MODEL
          else
            warn "${_vram_mib} MiB of GPU memory and AVA_MODEL is pinned to ${AVA_MODEL}."
            warn "If that is larger than ~$(( _vram_mib * 9 / 10240 )) GiB it will not load."
          fi
        fi
        ;;
    esac
  else
    warn "Profile is gpu but nvidia-smi is absent — falling back to cpu."
    AVA_PROFILE="cpu"
  fi
fi

# A driver is not a runtime. nvidia-smi proves the host can talk to the GPU; it
# proves nothing about `docker run --gpus all` working, which needs the NVIDIA
# container toolkit. Without it the vllm container dies at start with a message
# about an unknown runtime, which reads like an Ava bug.
#
# Deliberately OUTSIDE the sizing block: this applies to every gpu-profile box,
# including unified memory. It briefly lived inside the discrete-VRAM branch, and
# a GB10 with no container toolkit would then have sailed past it into a restart
# loop — the sizing question and the runtime question are independent.
if [ "$AVA_PROFILE" = "gpu" ] && [ "${AVA_SKIP_GPU_RUNTIME_CHECK:-0}" != "1" ]; then
  if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
    warn "The NVIDIA driver works, but Docker has no 'nvidia' runtime registered."
    warn "That is the NVIDIA Container Toolkit, installed separately from the driver:"
    warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
    warn "Falling back to the cpu profile. Re-run with AVA_SKIP_GPU_RUNTIME_CHECK=1 to override."
    AVA_PROFILE="cpu"
  fi
fi

# The ROCm equivalent of the container-toolkit check. A driver is not device
# access: the rocm profile bind-mounts /dev/kfd and /dev/dri into the container,
# and if the kernel driver is not loaded those nodes do not exist, so the service
# fails to start with a message about a missing device rather than about ROCm.
if [ "$AVA_PROFILE" = "rocm" ] && [ "${AVA_SKIP_GPU_RUNTIME_CHECK:-0}" != "1" ]; then
  if [ ! -e /dev/kfd ] || [ ! -e /dev/dri ]; then
    warn "No /dev/kfd or /dev/dri — the amdgpu kernel driver does not look loaded."
    warn "The rocm profile passes those devices through to Ollama, so it cannot start."
    warn "Install ROCm (or load amdgpu), then re-run. Falling back to the cpu profile."
    warn "Re-run with AVA_SKIP_GPU_RUNTIME_CHECK=1 to override."
    AVA_PROFILE="cpu"
  elif ! id -nG 2>/dev/null | tr ' ' '\n' | grep -qx render; then
    warn "You are not in the 'render' group; /dev/kfd access usually needs it."
    warn "  sudo usermod -aG render,video \$USER   # then log out and back in"
    warn "Continuing with the rocm profile — it may still work if permissions allow."
  fi
fi

# HAL-EXEMPT-END

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

# ── bare-metal path (Apple Silicon) ───────────────────────────────────────────
# Everything below this block is Docker. This runs the recipe deploy/README.md
# documents, in the same order, and stops at the same place the Docker path does.
if [ "$_BARE_METAL" = "1" ]; then
  cd "$AVA_DIR"
  command -v python3 >/dev/null 2>&1 \
    || die "python3 is required. Install it from python.org, or: brew install python@3.12"

  # Refuse an unsupported interpreter up front rather than failing three minutes
  # into a wheel build. pyproject.toml is the source of truth for the floor.
  python3 - <<'PY' || die "Python 3.12+ is required for the bare-metal path (pyproject.toml requires-python)."
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY

  if [ -n "${AVA_INSTALL_DRY_RUN:-}" ]; then
    say "AVA_INSTALL_DRY_RUN=1 — bare-metal path validated; not creating the venv."
    say "Would run: python3 -m venv .venv; pip install -r requirements.txt -e .; ava setup"
    exit 0
  fi

  if [ -d .venv ]; then
    say "Reusing the existing virtualenv at $AVA_DIR/.venv"
  else
    say "Creating a virtualenv at $AVA_DIR/.venv"
    python3 -m venv .venv || die "venv creation failed"
  fi
  # shellcheck disable=SC1091
  . .venv/bin/activate || die "could not activate .venv"

  say "Installing dependencies (this pulls torch for the voice extras — a few minutes)"
  python3 -m pip install --upgrade pip >/dev/null 2>&1 || true
  python3 -m pip install -r requirements.txt || die "pip install -r requirements.txt failed"
  python3 -m pip install -e . || die "pip install -e . failed"

  # An engine, because Ava is a control layer and has no brain of its own. Ollama
  # is the Mac default: it ships a Metal build, it is one brew away, and unlike
  # vLLM it actually runs here — `models.engine_servable_here('vllm')` returns
  # False on darwin-apple for exactly this reason.
  if command -v ollama >/dev/null 2>&1; then
    say "Found ollama on PATH."
  elif command -v brew >/dev/null 2>&1; then
    say "Installing ollama (brew)"
    brew install ollama || warn "brew install ollama failed — install it yourself: https://ollama.com/download"
  else
    warn "No ollama and no brew. Install an engine before first chat:"
    warn "  https://ollama.com/download    (or LM Studio, or llama.cpp)"
  fi

  say "Configuring Ava"
  ava setup || die "ava setup failed"
  ava doctor || warn "ava doctor reported problems — read them above before starting."

  cat <<EOF

Ava is installed at ${AVA_DIR}. To start it:

  cd "${AVA_DIR}" && . .venv/bin/activate && ava up

First run needs a model. Size it to your Mac (see docs/CHOOSE_A_MODEL.md):

  ollama serve &
  ollama pull llama3.1:8b

Then confirm the hardware readings on this machine, and help make Apple Silicon a
verified platform rather than a simulated one:

  python3 tools/ondevice_check.py --record

EOF
  exit 0
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

# The auto-detected profiles (gpu/cpu/rocm/cloud) all leave AVA_AGENT_ENABLED=0,
# because the agent runtime needs the NemoClaw CLI already present and Ava
# deliberately will not run a `curl | bash` installer on anyone's behalf. That is a
# defensible default — but landing there SILENTLY is not, because README.md sells
# tools, memory, connectors and self-editing with no caveat near the Quickstart.
# Say it once, here, where the profile was just chosen.
case "${AVA_PROFILE}" in
  agent|full) : ;;
  *)
    say ""
    say "Note: the '${AVA_PROFILE}' profile runs Ava WITHOUT the agent runtime, so"
    say "chat has no tools, no memory recall, no connectors and no self-editing."
    say "That is the default because the agent needs the NemoClaw CLI installed"
    say "first, and this script will not curl|bash one onto your machine."
    say "To enable it:  cp profiles/agent.env .env   (see deploy/README.md)"
    say ""
    ;;
esac

# The bridge answering is NOT the engine answering. /api/health returns ok
# unconditionally (phone_bridge.py) and never probes inference, and vllm carries
# `restart: on-failure:3`, so an OOM-looping engine is invisible here: the old
# script printed "Ava is up." over a chat box that errored on every message with
# no visible cause. Ask the engine directly.
if [ "$_ok" = 1 ] && [ "${AVA_PROFILE}" != "cpu" ] && [ "${AVA_PROFILE}" != "cloud" ]; then
  say "Checking the inference engine ..."
  _eng=0
  for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null --max-time 3 http://localhost:8002/v1/models 2>/dev/null; then
      _eng=1; break
    fi
    sleep 2
  done
  if [ "$_eng" != 1 ]; then
    warn "The bridge is up but the inference engine is NOT answering on :8002."
    warn "Chat will fail on every message until it does. Most likely it ran out of"
    warn "GPU memory loading the model — check with:"
    warn "  cd deploy && docker compose logs --tail 40 vllm"
    warn "Then either pick a smaller model (AVA_MODEL=...) or lower the context"
    warn "(AVA_VLLM_MAX_LEN=...), and re-run ./install.sh."
  fi
fi

if [ "$_ok" = 1 ]; then
  # First-run setup is gated on proving you can read the server's disk. Compose
  # publishes the port through the docker bridge, so the container sees the
  # gateway address (172.x.0.1) rather than 127.0.0.1 and a plain browser visit
  # is refused — correctly, but with a hint naming a CONTAINER path. We are on
  # the host and the data dir is bind-mounted, so read the token here and hand
  # over a link that just works. If a password was preset in .env there is no
  # token and no gate, and the plain URL is right.
  _claim_file="${AVA_HOME:-$AVA_DIR/deploy/ava-data}/data/setup_claim"
  _claim=""
  [ -r "$_claim_file" ] && _claim="$(tr -d '[:space:]' < "$_claim_file" 2>/dev/null || true)"

  # Reading it from the host is the fast path and it FAILS for most people. The image
  # declares no USER, so the container runs as root, and auth.py writes that file 0600
  # because it is a bearer token — both correct, and together they mean a normal user
  # on the host cannot read it through the bind mount. This failed SILENTLY: `[ -r ]`
  # is false, `_claim` stays empty, and the else branch below sent every non-root
  # Docker installer to a bare URL that answers 403, with a hint naming a path inside
  # the container. First run, primary documented install, dead end.
  #
  # So ask the container, which can always read its own file. AVA_HOME is pinned to
  # /data by docker-compose.yml and Ava keeps its stores in $AVA_HOME/data.
  #
  # MSYS_NO_PATHCONV=1: in Git Bash (the Windows path) MSYS rewrites any argument
  # that looks like a Unix absolute path into a Windows one, so /data/data/setup_claim
  # reaches the container as C:/Program Files/Git/data/data/setup_claim and the read
  # silently fails. That lands on the "could not read the token" branch below — the
  # last step of the documented install, turned into a dead end on Windows only.
  if [ -z "$_claim" ]; then
    _claim="$(MSYS_NO_PATHCONV=1 docker compose exec -T ava cat /data/data/setup_claim 2>/dev/null \
              | tr -d '[:space:]' || true)"
  fi
  # And if exec is unavailable, the bridge already printed the link on startup
  # (phone_bridge._startup, container-aware for this exact reason), so the logs carry
  # it. Belt and braces, because there is no second chance at a first run.
  if [ -z "$_claim" ]; then
    _claim="$(docker compose logs ava 2>/dev/null \
              | sed -n 's|.*/setup?claim=\([A-Za-z0-9_-]\{8,\}\).*|\1|p' | tail -1)"
  fi

  if [ -n "$_claim" ]; then
    _link="http://localhost:8096/setup?claim=$_claim"
    say "Ava is up. Opening your browser to set an admin password:"
    say "  $_link"
    # Also on its own line: terminals wrap long URLs, and a wrapped link that is
    # copied in two pieces loses the query string — which on this URL is the only
    # part that matters. The bare token can be retyped.
    say "  (token alone, if that link wrapped: $_claim)"
    say "(the link is single-use — it stops working the moment a password is set)"
    open_browser "$_link"
  elif MSYS_NO_PATHCONV=1 docker compose exec -T ava test -f /data/data/setup_claim 2>/dev/null; then
    # A token EXISTS and we could not get it. That is not the same as "no gate", and
    # saying "sign in" here is how a first run becomes a 403 with no way forward.
    warn "Ava is up, but this shell could not read the first-run token."
    warn "Get your link with:"
    warn "  cd $AVA_DIR/deploy && docker compose logs ava | grep 'setup?claim='"
  else
    # No token because a password was preset in .env — no gate, so the plain URL is
    # right.
    say "Ava is up. Open http://localhost:8096 and sign in."
  fi
else
  warn "Ava did not answer on http://localhost:8096 within ~3 minutes."
  warn "It may still be pulling a model. Check with:"
  warn "  cd $AVA_DIR/deploy && docker compose logs -f ava"
  warn "  cd $AVA_DIR/deploy && docker compose ps"
fi
say "Logs:  cd $AVA_DIR/deploy && docker compose logs -f ava"
