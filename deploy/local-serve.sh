#!/usr/bin/env bash
# local-serve.sh — start Ava's local always-on brain on vLLM, for ANY model.
#
# Ava keeps ONE model resident at all times (no scaling models up/down). This
# script (re)starts a single vLLM container serving whatever model you point it
# at. It is the NVIDIA/vLLM launch path; on Apple Silicon or CPU-only boxes
# `ava setup` seeds an Ollama backend instead and you never run this.
#
# ── Serve your own model ──────────────────────────────────────────────────────
#   AVA_MODEL=Qwen/Qwen3-32B-AWQ bash deploy/local-serve.sh
#
# or set AVA_MODEL in .env and just run `bash deploy/local-serve.sh`.
#
# The two flags that MUST match the model are --tool-call-parser and
# --reasoning-parser. Getting them wrong does not raise an error: vLLM returns
# NO tool_calls, the call falls through as plain text, the agent never sees it,
# and turns loop until they time out — it looks like Ava "not responding".
# So this script resolves them from a per-family table below rather than letting
# you inherit another model's parsers by accident, and REFUSES TO GUESS for a
# family it doesn't know (it warns and serves without tool-calling instead of
# silently mis-parsing). Override either one explicitly at any time:
#
#   AVA_TOOL_PARSER=hermes  AVA_REASONING_PARSER=  bash deploy/local-serve.sh
#
# An empty value means "pass no parser flag"; unset means "use the table".
# Parser names come from the model card's vLLM section — check there first.
#
# ── One-time / after reboot ───────────────────────────────────────────────────
#   bash deploy/local-serve.sh
#
# The container restarts on Docker/host restart by default (AVA_SERVE_RESTART).
# If Ava's allocator manages this container, set AVA_SERVE_RESTART=no so there is
# only one supervisor — see the note by RESTART below.
#
# NOTE: with AVA_SERVE_RESTART=no, nothing brings the container back after a reboot
# (the allocator restores only what it released itself). Pair it with a boot unit that
# runs THIS script — Type=oneshot + RemainAfterExit, so systemd starts it and then stops
# caring, and will not fight the allocator when it stops the container for a render.
# `ava-omni.service` on the development box is exactly that.
set -uo pipefail

# Resolve the repo root and load .env (HF_CACHE, AVA_HOME, ports …) if present,
# so this script has no hardcoded personal paths.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# .env fills in what the environment has NOT already set — the same precedence
# ava_bridge/settings.py uses (os.environ.setdefault). A plain `set -a; . .env`
# gives the FILE the last word, which silently defeats the override this script's
# own header documents (`AVA_MODEL=… bash deploy/local-serve.sh`) the moment
# someone pins AVA_MODEL in .env. Snapshot the real environment, source, restore.
if [ -f "$REPO/.env" ]; then
  _ava_real_env="$(export -p)"
  set -a; . "$REPO/.env"; set +a
  eval "$_ava_real_env"
  unset _ava_real_env
fi

# The image is resolved from the host architecture, not pinned to the maintainer's.
# It used to default to the aarch64 CUDA-13 tag unconditionally, with a comment
# telling x86_64 users to override — so the documented default did not even PULL
# on the majority of NVIDIA hardware. A default that is wrong for most readers is
# a broken default, however well commented.
case "$(uname -m 2>/dev/null || true)" in
  aarch64|arm64) _vllm_default="vllm/vllm-openai:v0.20.0-aarch64-cu130-ubuntu2404" ;;
  *)             _vllm_default="vllm/vllm-openai:latest" ;;
esac
IMAGE="${VLLM_IMAGE:-$_vllm_default}"

# Accelerator flags for `docker run`, by vendor. vLLM needs a CUDA or ROCm device;
# there is no CPU story worth serving here, so an unknown accelerator refuses
# rather than starting a container that will OOM or hang.
_gpu_args() {
  if command -v nvidia-smi >/dev/null 2>&1; then
    printf '%s' "--gpus all"
  elif [ -e /dev/kfd ] && [ -e /dev/dri ]; then
    # ROCm: device nodes plus the groups that own them. Untested on device —
    # see the linux-amd-* rows in deploy/platforms.conf.
    printf '%s' "--device /dev/kfd --device /dev/dri --group-add video --group-add render"
  else
    printf '%s' ""
  fi
}
GPU_ARGS="$(_gpu_args)"
# Hugging Face cache the container mounts. Defaults to the model store under
# AVA_HOME (matching `ava setup`); override with HF_CACHE or AVA_HOME in .env.
HF_CACHE="${HF_CACHE:-${AVA_HOME:-$REPO}/models/hf}"
# AVA_MODEL is the canonical name; AVA_OMNI_MODEL is kept as a legacy alias so
# existing .env files and the ava-omni.service unit keep working unchanged.
MODEL="${AVA_MODEL:-${AVA_OMNI_MODEL:-Qwen/Qwen2.5-7B-Instruct}}"
PORT="${AVA_SERVE_PORT:-${OMNI_PORT:-8002}}"
# Container name. Things outside this repo may reference it by name (a sibling
# app's GPU coordinator, your own connectors/, ava_security_check.py), so rename
# it only if you update those too.
NAME="${AVA_SERVE_CONTAINER:-vllm-open}"
RESTART="${AVA_SERVE_RESTART:-${OMNI_RESTART:-unless-stopped}}"
# Docker's restart policy vs the allocator: they are both supervisors, and only one
# can decide when there is room. `unless-stopped` (the default here, right for a box
# with no allocator) retries a start forever with no backoff — the mechanism behind the
# 7997 restarts this box once saw. If you declare this container in ava.yaml
# `alloc.models`, set AVA_SERVE_RESTART=no so Ava is the sole supervisor; `ava doctor`
# flags the combination.
# CTX is resolved below, from AVA_SERVE_MAX_LEN clamped to the model's real
# context. It must exceed the agent's system-context (~29k tokens of persona +
# tool schemas), and the resolver warns if it does not.

# ── Per-model vLLM flags ──────────────────────────────────────────────────────
# Resolved from deploy/model-flags.conf, which is the single source of truth for
# every caller (this script, deploy/install.sh, and the compose vllm service).
# It used to be an inline `case` here, which meant compose carried a second,
# already-drifting copy that could not express a reasoning parser at all.
#
# The resolver keeps the semantics this script always had: explicit env beats the
# table, "set to empty" means deliberately pass no flag, and an unknown family
# serves WITHOUT tool-calling rather than guessing a parser.
# shellcheck source=deploy/resolve-model-flags.sh
. "$HERE/resolve-model-flags.sh"
ava_resolve_model_flags "$MODEL"

family="$AVA_RESOLVED_FAMILY"
TOOL_PARSER="$AVA_RESOLVED_TOOL_PARSER"
REASON_PARSER="$AVA_RESOLVED_REASONING_PARSER"
EXTRA_FLAGS="$AVA_RESOLVED_EXTRA_FLAGS"
# Clamped to the checkpoint's real context when the table knows it: vLLM RAISES
# rather than clamping when asked for more than the model supports.
CTX="$AVA_RESOLVED_MAX_LEN"

# --gpu-memory-utilization. 0.40 (~48.7 GiB of a 121.7 GiB unified pool) is this
# box's measured value for the 30B Omni, lowered from 0.55 on 2026-07-25 after
# 7997 restarts: at 0.55 vLLM took 66.9 GiB and left only ~34 GiB MemAvailable,
# so a FLUX.2 render (66.5 GiB resident) could not co-fit and vLLM's startup
# check failed on a loop. At 0.40 it still measures KV 9.7 GiB / 676k tokens /
# 40.8x concurrency at the full 65k ctx — ample for a single user.
#
# Sizing KV is about CONCURRENCY, not context: vLLM needs only >=1 sequence's
# worth of KV to accept --max-model-len. Do NOT trade CTX for headroom.
#
# READING THE NUMBERS when you retune this: use MemAvailable from /proc/meminfo,
# NOT the GPU service's /system_stats vram_free. On a unified-memory host vram_free tracks
# MemFree and so ignores ~30 GiB of reclaimable page cache (reading FLUX.2's 66 GiB
# of weights fills it), which makes it read 5-9 GiB when 34 GiB is genuinely
# available. Both numbers move for unrelated reasons, and neither is comparable
# across a render unless you drop caches first.
#
# A model this size and a FLUX.2 render CANNOT co-fit at any utilization — that is
# what a GPU timeshare coordinator is for: pause this container around the render,
# ref-counted and debounced, so a burst of renders costs one reload.
# 0.40 does leave room for the GPU model previews (6-25 GB) and kokoro TTS to co-fit.
#
# If you serve a different model or have a dedicated GPU (no renders competing
# for the pool), raise this — 0.85-0.90 is the usual vLLM default territory.
UTIL="${AVA_SERVE_GPU_UTIL:-${OMNI_GPU_UTIL:-0.40}}"

# Stop any prior container of the same name before starting a new one.
# Skipped under AVA_SERVE_DRY_RUN so checking your flags never disturbs a live one.
if [ -z "${AVA_SERVE_DRY_RUN:-}" ]; then
  docker rm -f "$NAME" 2>/dev/null || true
fi

echo "[local-serve] Starting $NAME on :$PORT"
echo "[local-serve]   model=$MODEL"
echo "[local-serve]   family=${family:-unknown} tool-parser=${TOOL_PARSER:-<none>} reasoning-parser=${REASON_PARSER:-<none>}"
echo "[local-serve]   util=$UTIL ctx=$CTX restart=$RESTART"

# vLLM auto-detects quantization (FP8/AWQ/GPTQ) from the checkpoint config; no
# --quantization flag needed. If a vLLM version complains, add --quantization <fmt>
# via AVA_SERVE_EXTRA_FLAGS.
set -- --model "${MODEL}" \
  --trust-remote-code \
  --dtype auto \
  --max-model-len "${CTX}" \
  --gpu-memory-utilization "${UTIL}" \
  --enable-prefix-caching
# EXTRA_FLAGS is a deliberate word-split flag list, not a single argument.
# shellcheck disable=SC2086
[ -n "$EXTRA_FLAGS" ] && set -- "$@" $EXTRA_FLAGS
if [ -n "$TOOL_PARSER" ]; then
  set -- "$@" --enable-auto-tool-choice --tool-call-parser "$TOOL_PARSER"
fi
[ -n "$REASON_PARSER" ] && set -- "$@" --reasoning-parser "$REASON_PARSER"

# AVA_SERVE_DRY_RUN=1 prints the resolved vLLM args and exits without touching
# Docker — check your parsers are right before committing to a multi-minute load.
if [ -n "${AVA_SERVE_DRY_RUN:-}" ]; then
  echo "[local-serve] DRY RUN — would exec:"
  printf '  arch=%s  image=%s\n' "$(uname -m 2>/dev/null || echo '?')" "$IMAGE"
  printf '  accelerator args: %s\n' "${GPU_ARGS:-<none detected>}"
  printf '  docker run -d %s --name %s %s\n' "$GPU_ARGS" "$NAME" "$IMAGE"
  printf '    %s\n' "$@"
  exit 0
fi

if [ -z "$GPU_ARGS" ]; then
  echo "[local-serve] No CUDA or ROCm device found. vLLM has no useful CPU mode," >&2
  echo "[local-serve] so this would start a container that cannot serve." >&2
  echo "[local-serve] Use Ollama instead: cd deploy && cp profiles/cpu.env .env && docker compose up -d" >&2
  echo "[local-serve] (Set AVA_SERVE_FORCE_NO_GPU=1 to try anyway.)" >&2
  [ "${AVA_SERVE_FORCE_NO_GPU:-0}" = "1" ] || exit 1
fi

# GPU_ARGS is a deliberate multi-word flag list, so it must word-split.
# shellcheck disable=SC2086
docker run -d $GPU_ARGS --shm-size=32g \
  --restart "$RESTART" \
  -v "${HF_CACHE}:/root/.cache/huggingface" \
  -p "${AVA_SERVE_BIND:-${OMNI_BIND:-127.0.0.1}}:${PORT}:8000" \
  --name "$NAME" \
  "${IMAGE}" \
  "$@"

echo "[local-serve] Waiting for readiness on :$PORT (bounded ~15 min) ..."
deadline=$(( $(date +%s) + 900 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    echo "[local-serve] $NAME READY → http://localhost:${PORT}/v1 ($MODEL)"
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep "$NAME" || true
    exit 0
  fi
  st=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || echo gone)
  if [ "$st" != running ]; then
    echo "[local-serve] ERROR: container is '$st' — last logs:"
    docker logs --tail 30 "$NAME" 2>&1 | tail -30
    exit 1
  fi
  docker logs "$NAME" 2>&1 | grep -E "shards|Application startup|ERROR|out of memory" | tail -1 || true
  sleep 6
done
echo "[local-serve] TIMEOUT waiting for readiness; check: docker logs $NAME"
exit 1
