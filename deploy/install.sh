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
#   AVA_PROFILE   cpu|cuda|gpu|rocm|cloud|agent (default: auto-detected)
#   AVA_MODEL     override the model the profile ships with
#   AVA_REPO      git URL to clone from    (only when NOT run inside a clone)
#   AVA_INSTALL_DRY_RUN=1   write .env and stop, touching no images (for CI)
#   AVA_FORCE_DOCKER=1      use Docker on an Apple-Silicon Mac anyway
#   AVA_SKIP_GPU_RUNTIME_CHECK=1  skip the nvidia-container-toolkit probe
#   AVA_NO_BROWSER=1        do not open the first-run link (it is still printed)
set -euo pipefail

AVA_DIR="${AVA_DIR:-$HOME/ava}"
AVA_REPO="${AVA_REPO:-}"
_PROFILES="cpu cuda gpu rocm cloud agent"

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

  # ...and neither line above needs a RUNNING Docker. `docker` is on PATH from
  # the moment Docker Desktop is installed, and `docker compose version` is
  # answered by the client alone — so an install with the engine merely stopped
  # cleared both, spent a minute probing hardware and writing .env, and died at
  # `docker compose up` with a raw npipe/socket error.
  #
  # The wasted minute is the small half. Every probe in between that ASKS THE
  # DAEMON a question read its silence as an answer: the container-runtime probe
  # below announced "Docker has no 'nvidia' runtime registered", sent the owner
  # of a perfectly good 6 GB card away to install the NVIDIA Container Toolkit
  # (from a Linux package-manager URL, on Windows), and wrote the cpu profile
  # into .env on the way past. Two confident wrong diagnoses and a silently
  # downgraded install, all from one question nobody actually asked.
  #
  # So it is asked once, here, first. Nothing this script does afterwards works
  # without the daemon, which makes this the only honest place to stop.
  if ! _docker_err="$({ docker info >/dev/null; } 2>&1)"; then
    warn "Docker is installed, but its daemon is not answering:"
    printf '%s\n' "$_docker_err" | awk 'NF && n < 2 { print "    " $0; n++ }' >&2
    case "$_docker_err" in
      *"permission denied"*)
        warn "That is a permissions problem, not a stopped engine — your user"
        warn "cannot reach the socket. Add yourself to the 'docker' group, then"
        warn "log out and back in (a new terminal is not enough):"
        warn "  sudo usermod -aG docker \$USER" ;;
      *)
        case "$(uname -s 2>/dev/null || true)" in
          MINGW*|MSYS*|CYGWIN*|Darwin)
            warn "Start Docker Desktop, wait until it reports Running, and run this"
            warn "again." ;;
          *)
            warn "Start it, then run this again:"
            warn "  sudo systemctl start docker" ;;
        esac ;;
    esac
    die "Docker's daemon is not reachable — nothing can be built or started."
  fi
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

# ── The host GPU, measured once, before anything decides anything about it ────
# This used to be a `_vram_mib` local inside the gpu branch: read, printed,
# branched on, then dropped on the floor. Three later steps need it — the
# container-runtime gate, the profile branch, and the managed .env block this
# script writes at the end — so it is read here, once, and kept.
_nvidia_smi=0
_host_gpu_name=""
_host_gpu_vram_mib=""
if command -v nvidia-smi >/dev/null 2>&1; then
  _nvidia_smi=1
  # Name and memory from ONE query so the two describe the SAME card. Asking
  # separately takes the first card's name and the largest card's memory, which
  # on a multi-GPU box describes a machine that does not exist.
  # `tr -d '\r'`: this script runs under Git Bash on Windows (install.cmd hands
  # it there), where nvidia-smi is a native .exe and ends every line CRLF. Only
  # the LAST field carries the \r, which today is the number the digit-strip
  # below cleans anyway — so this is defence, not a fix: reorder the query, or
  # meet a driver that answers one column, and the \r lands in a NAME that gets
  # written into deploy/.env and handed to the container.
  _smi_rows="$(nvidia-smi --query-gpu=name,memory.total \
               --format=csv,noheader,nounits 2>/dev/null | tr -d '\r' || true)"
  _host_gpu_name="$(printf '%s\n' "$_smi_rows" | awk -F',' '
      NF > 1 {
        n = $1; gsub(/^[ \t]+|[ \t]+$/, "", n)
        m = $2; gsub(/[^0-9]/, "", m)
        if (pick == "") pick = n          # first card, if none reports memory
        if (m != "" && m + 0 > best) { best = m + 0; pick = n }
      }
      END { print pick }')"
  # Largest card wins: "does the model fit" is a question about ONE device.
  # Unified memory answers N/A here BY DESIGN, so this stays empty there rather
  # than becoming a zero — the `case` below still treats non-numeric as unknown.
  _host_gpu_vram_mib="$(printf '%s\n' "$_smi_rows" | awk -F',' '
      { m = $NF; gsub(/[^0-9]/, "", m); if (m != "" && m + 0 > best) best = m + 0 }
      END { if (best > 0) print best }')"
fi

# ── Can Docker actually hand that GPU to a container? ────────────────────────
# A driver is not a runtime. nvidia-smi proves the HOST can talk to the GPU; it
# proves nothing about `docker run --gpus all` working, which needs the NVIDIA
# Container Toolkit. Without it a GPU-reserving service dies at start with a
# message about an unknown runtime, which reads like an Ava bug.
#
# This ran AFTER the profile was chosen and only when the profile was already
# `gpu`. So a box that had just been dumped to `cpu` for having a small card was
# never asked whether its Docker could reach the GPU at all — and on the machine
# that prompted this change, it could. The answer now DECIDES the branch below,
# so it has to be known first. It is asked for any box with an NVIDIA card.
_nvidia_docker="unknown"
if [ "$_BARE_METAL" = "0" ] && { [ "$_nvidia_smi" = "1" ] || [ "$AVA_PROFILE" = "gpu" ]; }; then
  if [ "${AVA_SKIP_GPU_RUNTIME_CHECK:-0}" = "1" ]; then
    _nvidia_docker="yes"        # the owner overrode the probe; take their word
  elif _runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null)"; then
    case "$_runtimes" in
      *nvidia*) _nvidia_docker="yes" ;;
      *)        _nvidia_docker="no"  ;;
    esac
  fi
  # Deliberately no `else`. A `docker info` that FAILED has not reported an
  # absent runtime, it has reported nothing, and `unknown` is the value for
  # that. This was `docker info ... 2>/dev/null | grep -q nvidia`, where an
  # unreachable daemon and a daemon with no NVIDIA runtime produce the identical
  # empty output — so a stopped Docker Desktop reached the owner as a missing
  # NVIDIA Container Toolkit. The preflight above stops that case before it can
  # get here; keeping the two questions apart is what stops it coming back.
fi

if [ "$AVA_PROFILE" = "gpu" ] && [ "${AVA_DETECTED_MEM_MODEL:-}" = "unified" ]; then
  # On unified memory the VRAM query returns N/A BY DESIGN, so the discrete-VRAM
  # sizing gate below does not apply and warning that it "could not read GPU
  # memory" is noise about a reading that cannot exist. Model fit on this class is
  # a system-RAM question, which model_fit.py already answers at runtime.
  say "Unified memory — skipping the discrete-VRAM sizing check (fit is a system-RAM question here)."
elif [ "$AVA_PROFILE" = "gpu" ]; then
  if [ "$_nvidia_smi" = "1" ]; then
    _vram_mib="$_host_gpu_vram_mib"
    case "$_vram_mib" in
      ''|*[!0-9]*) warn "Could not read GPU memory from nvidia-smi — assuming the gpu profile fits." ;;
      *)
        say "Detected GPU memory: ${_vram_mib} MiB${_host_gpu_name:+ (${_host_gpu_name})}"
        # ARITHMETIC, not a remembered number. The old gate was 16000 MiB, which
        # green-lit an RTX 4080 (16376 MiB) for a model that cannot serve on it:
        #
        #   a 7B model at BF16             14.2 GiB
        #   usable at --gpu-memory-utilization 0.90 of 16376 MiB
        #                                  14.4 GiB
        #   -> the weights fit with 0.2 GiB spare, and a 32768-token KV cache
        #      needs ~2 GiB, so vLLM OOMs on load.
        #
        # And the context cannot simply be shortened: 32768 is what clears the
        # ~29k tokens Ava's own system prompt and tool schemas occupy. A shorter
        # context breaks the agent, so on a card this size the honest lever is a
        # SMALLER MODEL, not a smaller window.
        #
        #   weights + KV at 0.90  =>  (14.2 + 2) / 0.90  =  18.0 GiB
        #
        # Tiers, so a card gets the best engine it can actually run instead of
        # being dumped to CPU. The two below are vLLM's, and are not to be
        # relaxed — they are what stops an RTX 4080 from OOMing on load:
        _need_default_mib=18000
        _need_small_mib=12000
        # A FOURTH number, and a different question. Both figures above size vLLM
        # serving FP16, which is right for vLLM — but the branch under them lands
        # on OLLAMA, which serves quantized GGUF, and applying an FP16 floor to a
        # Q4 engine is how a perfectly usable 6 GB card got declared useless and
        # handed a CPU-only Ollama several times slower for the same answer.
        #
        # Same arithmetic discipline, for the model profiles/cuda.env ships:
        #
        #   llama3.2 (3B) Q4_K_M weights                       2.0 GiB
        #   KV cache, 8192 tokens, fp16
        #     (28 layers x 8 KV heads x 128 dim x 2 x 2 B
        #      = 0.109 MiB/token, x 8192 tokens)               0.9 GiB
        #   CUDA context + compute/graph buffers               0.6 GiB
        #   framebuffer a laptop card already spends driving
        #     the screen it is attached to                     0.5 GiB
        #   ------------------------------------------------------------
        #   weights + KV + context + headroom                  4.0 GiB
        #
        #   -> 4.0 GiB = 4096 MiB
        _need_quantized_mib=4096
        if [ "$_vram_mib" -lt "$_need_quantized_mib" ]; then
          warn "Only ${_vram_mib} MiB of GPU memory — under the ${_need_quantized_mib} MiB a quantized"
          warn "3B needs to sit entirely on the card, so falling back to the cpu"
          warn "profile. Ollama on CPU is slow but it actually starts."
          AVA_PROFILE="cpu"
        elif [ "$_vram_mib" -lt "$_need_small_mib" ]; then
          # The card is too small for vLLM and big enough for Ollama. That is a
          # real GPU install, not a fallback — but only if the daemon can pass the
          # device through, because profiles/cuda.env starts a service that
          # RESERVES it and compose refuses to start such a service otherwise.
          if [ "$_nvidia_docker" = "yes" ]; then
            AVA_PROFILE="cuda"
            say "${_vram_mib} MiB is under the ${_need_small_mib} MiB vLLM needs even for a 3B in FP16,"
            say "but comfortably over the ${_need_quantized_mib} MiB a quantized model needs. Using the"
            say "cuda profile: Ollama with CUDA, serving GGUF weights on your GPU."
            if [ -n "${AVA_MODEL:-}" ]; then
              warn "AVA_MODEL is pinned to ${AVA_MODEL}, which will be passed to Ollama as a"
              warn "TAG (llama3.1:8b), not a Hugging Face repo id. Unset it to take the"
              warn "profile's default, or pin an Ollama tag."
            fi
          elif [ "$_nvidia_docker" = "no" ]; then
            warn "${_vram_mib} MiB would serve a quantized model on the GPU (the cuda profile),"
            warn "but Docker has no 'nvidia' runtime registered, so no container here can"
            warn "reach the card. That is the NVIDIA Container Toolkit, installed separately"
            warn "from the driver:"
            warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
            warn "Falling back to the cpu profile. Install it and re-run to use the GPU,"
            warn "or re-run with AVA_SKIP_GPU_RUNTIME_CHECK=1 to override this probe."
            AVA_PROFILE="cpu"
          else
            # `unknown` — the daemon never answered, so this says what it knows
            # rather than blaming a toolkit nobody checked for. The same value
            # is treated the same way by the gpu-profile gate below; a card is
            # given up for a fact, never for a silence.
            warn "${_vram_mib} MiB would serve a quantized model on the GPU, but Docker did"
            warn "not answer when asked whether it can reach the card. Falling back to the"
            warn "cpu profile — re-run once Docker is healthy to use the GPU."
            AVA_PROFILE="cpu"
          fi
        elif [ "$_vram_mib" -lt "$_need_default_mib" ]; then
          # A 12-18 GB card: keep the GPU, and say what will fit on it.
          #
          # This used to SUBSTITUTE a model — silently switching an unset
          # AVA_MODEL to a 3B of Ava's choosing. Ava no longer picks models:
          # the number below is the useful half of that branch (what this card
          # can hold), and choosing against it is the owner's.
          warn "${_vram_mib} MiB of GPU memory: a model larger than about"
          warn "~$(( _vram_mib * 9 / 10240 )) GiB of weights will not load with a 32k context."
          if [ -n "${AVA_MODEL:-}" ]; then
            warn "AVA_MODEL is set to ${AVA_MODEL} — check it against that figure."
          else
            warn "Pick one that fits: see docs/CHOOSE_A_MODEL.md, then re-run with"
            warn "  AVA_MODEL=<model-id> ./install.sh"
          fi
        fi
        ;;
    esac
  else
    warn "Profile is gpu but nvidia-smi is absent — falling back to cpu."
    AVA_PROFILE="cpu"
  fi
fi

# The gpu profile's half of the container-toolkit question, unchanged in effect:
# vLLM reserves the device too, so no runtime means no start. The PROBE moved
# above the sizing block (its answer now picks between cuda and cpu); this is
# just the consequence for `gpu`, and `unknown` cannot fire it — that value means
# nobody asked, which is not the same as "no".
#
# Deliberately OUTSIDE the sizing block: this applies to every gpu-profile box,
# including unified memory. It briefly lived inside the discrete-VRAM branch, and
# a GB10 with no container toolkit would then have sailed past it into a restart
# loop — the sizing question and the runtime question are independent.
if [ "$AVA_PROFILE" = "gpu" ] && [ "$_nvidia_docker" = "no" ]; then
  warn "The NVIDIA driver works, but Docker has no 'nvidia' runtime registered."
  warn "That is the NVIDIA Container Toolkit, installed separately from the driver:"
  warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
  warn "Falling back to the cpu profile. Re-run with AVA_SKIP_GPU_RUNTIME_CHECK=1 to override."
  AVA_PROFILE="cpu"
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
  if [ "${AVA_PROFILE}" = "gpu" ] || [ "${AVA_PROFILE}" = "agent" ]; then
    bash ./resolve-model-flags.sh --env "$_model" 2>/dev/null || true
  fi
  # The host GPU this script MEASURED, declared so the container can report it.
  #
  # Nothing inside the bridge container can discover this: compose reserves the
  # device for the inference service only, so the bridge's own probes correctly
  # find no GPU — which is why Setup → Hardware says "no GPU under Docker" on a
  # machine with a working card. This script is the one process that stands on
  # the host and reads the truth, and it used to print the number and let it die
  # as a shell local. Declaring it is the only way it crosses the boundary.
  #
  # Written only when actually measured. An absent line means "not declared",
  # which a consumer must not read as "no GPU" — and a `0` would say exactly the
  # wrong thing on unified memory, where memory.total answers N/A by design.
  [ -n "$_host_gpu_name" ] && printf 'AVA_DECLARED_HOST_GPU_NAME=%s\n' "$_host_gpu_name"
  [ -n "$_host_gpu_vram_mib" ] && printf 'AVA_DECLARED_HOST_GPU_VRAM_MIB=%s\n' "$_host_gpu_vram_mib"
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

# Ollama's model store starts empty and nothing else fills it, so an install that
# stopped here would present a chat box wired to a server with no model.
#
# Gated on the ENGINE, not the profile. It read `= "cpu"`, but rocm is equally
# Ollama-backed (profiles/rocm.env sets AVA_BACKEND_ENGINE=ollama and the same
# AVA_MODEL) on the `ollama-rocm` service — so every AMD install finished with an
# empty store and a dead chat box, which is the exact failure the line above says
# this exists to prevent. Nobody caught it because AMD is ci-simulated: no runner
# has this hardware, and the maintainer does not own any.
case "$AVA_PROFILE" in
  cpu)  _ollama_svc="ollama" ;;
  cuda) _ollama_svc="ollama-cuda" ;;
  rocm) _ollama_svc="ollama-rocm" ;;
  *)    _ollama_svc="" ;;   # gpu/agent serve through vLLM, which loads at boot
esac
if [ -n "$_ollama_svc" ]; then
  _ollama_model="$(grep -E '^AVA_MODEL=' "$_ENV" | tail -1 | cut -d= -f2-)"
  if [ -n "$_ollama_model" ]; then
    say "Pulling ${_ollama_model} into Ollama (first run only; this takes a while)"
    docker compose exec -T "$_ollama_svc" ollama pull "$_ollama_model" \
      || warn "Could not pull ${_ollama_model} — run: docker compose exec ${_ollama_svc} ollama pull ${_ollama_model}"
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

# The auto-detected profiles (gpu/cuda/cpu/rocm/cloud) all leave AVA_AGENT_ENABLED=0,
# because the agent runtime needs the NemoClaw CLI already present and Ava
# deliberately will not run a `curl | bash` installer on anyone's behalf. That is a
# defensible default — but landing there SILENTLY is not, because README.md sells
# tools, memory, connectors and self-editing with no caveat near the Quickstart.
# Say it once, here, where the profile was just chosen.
case "${AVA_PROFILE}" in
  agent) : ;;
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
#
# WHICH engine, on WHICH port, comes from the profile rather than being assumed.
# This probed :8002 and named `vllm` for every profile that was not cpu or cloud,
# so a rocm install — which serves Ollama on :11434 — was told its engine was
# dead and sent to the logs of a service that profile never starts. cuda would
# have inherited exactly the same wrong answer.
_eng_port=""
_eng_svc=""
case "$AVA_PROFILE" in
  cpu|cloud) : ;;   # cloud is someone else's uptime; cpu just proved itself by pulling
  cuda|rocm) _eng_port="11434"; _eng_svc="$_ollama_svc" ;;
  *)         _eng_port="8002";  _eng_svc="vllm" ;;
esac
if [ "$_ok" = 1 ] && [ -n "$_eng_port" ]; then
  say "Checking the inference engine ..."
  _eng=0
  for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null --max-time 3 "http://localhost:${_eng_port}/v1/models" 2>/dev/null; then
      _eng=1; break
    fi
    sleep 2
  done
  if [ "$_eng" != 1 ]; then
    warn "The bridge is up but the inference engine is NOT answering on :${_eng_port}."
    warn "Chat will fail on every message until it does. Most likely it ran out of"
    warn "GPU memory loading the model — check with:"
    warn "  cd deploy && docker compose logs --tail 40 ${_eng_svc}"
    if [ "$_eng_svc" = "vllm" ]; then
      warn "Then either pick a smaller model (AVA_MODEL=...) or lower the context"
      warn "(AVA_VLLM_MAX_LEN=...), and re-run ./install.sh."
    else
      warn "Then pick a smaller model (AVA_MODEL=<an ollama tag>) and re-run ./install.sh,"
      warn "or drop to the cpu profile: cp profiles/cpu.env .env && docker compose up -d"
    fi
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
