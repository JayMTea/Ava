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
[ -f "$REPO/.env" ] && set -a && . "$REPO/.env" && set +a

# Default image is aarch64 (DGX/GB10-class hosts). On x86_64 override it:
#   VLLM_IMAGE=vllm/vllm-openai:latest ./deploy/local-serve.sh
IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.20.0-aarch64-cu130-ubuntu2404}"
# Hugging Face cache the container mounts. Defaults to the model store under
# AVA_HOME (matching `ava setup`); override with HF_CACHE or AVA_HOME in .env.
HF_CACHE="${HF_CACHE:-${AVA_HOME:-$REPO}/models/hf}"
# AVA_MODEL is the canonical name; AVA_OMNI_MODEL is kept as a legacy alias so
# existing .env files and the ava-omni.service unit keep working unchanged.
MODEL="${AVA_MODEL:-${AVA_OMNI_MODEL:-nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8}}"
PORT="${AVA_SERVE_PORT:-${OMNI_PORT:-8002}}"
# Container name. Left as `vllm-open` by default because things OUTSIDE this repo
# reference it by name — note-keeper's timeshare coordinator
# (STUDIO_VIDEO_SUPER), connectors/vllm-open/, and ava_security_check.py. Rename
# it only if you update those too.
NAME="${AVA_SERVE_CONTAINER:-vllm-open}"
RESTART="${AVA_SERVE_RESTART:-${OMNI_RESTART:-unless-stopped}}"
# Docker's restart policy vs the allocator: they are both supervisors, and only one
# can decide when there is room. `unless-stopped` (the default here, right for a box
# with no allocator) retries a start forever with no backoff — the mechanism behind the
# 7997 restarts this box once saw. If you declare this container in ava.yaml
# `alloc.models`, set AVA_SERVE_RESTART=no so Ava is the sole supervisor; `ava doctor`
# flags the combination.
CTX="${AVA_SERVE_MAX_LEN:-${OMNI_MAX_LEN:-65536}}"   # must exceed the agent's system-context (~29k tokens of persona + tool schemas)

# ── Per-model vLLM flags ──────────────────────────────────────────────────────
# Keyed by a substring of the model id, lowercased. Each family sets the tool /
# reasoning parsers and any extra flags that model NEEDS to boot or behave.
#
# Sourced from each model card's vLLM section. If vLLM rejects a name, check the
# card — do not just drop the flag, because dropping it silently disables tool
# calling rather than failing loudly.
lc_model="$(printf '%s' "$MODEL" | tr '[:upper:]' '[:lower:]')"
family=""; tbl_tool=""; tbl_reason=""; tbl_extra=""

case "$lc_model" in
  *nemotron*omni*)
    family="nemotron-omni"
    # Omni emits tool calls in the qwen3_coder XML format (<function=name>
    # <parameter=x>…), NOT hermes JSON — using `hermes` here returns no tool_calls.
    tbl_tool="qwen3_coder"; tbl_reason="nemotron_v3"
    # --enforce-eager: this model captures CUDA graphs across 512 batch sizes
    #   INCLUDING its vision encoder; that transient spike OOM-killed the engine on
    #   a 121 GB unified pool. Disabling capture costs ~5-10% decode speed and is
    #   what makes the always-on load fit.
    # --max-num-batched-tokens 4096: NemotronH's Mamba cache alignment requires
    #   this to be >= block_size (2128); the 2048 default trips an assert at boot.
    tbl_extra="--enforce-eager --max-num-batched-tokens 4096"
    ;;
  *nemotron*)
    family="nemotron"; tbl_tool="hermes"; tbl_reason="nemotron_v3" ;;
  *qwen3-coder*|*qwen3_coder*)
    family="qwen3-coder"; tbl_tool="qwen3_coder"; tbl_reason="" ;;
  *qwen3*)
    family="qwen3"; tbl_tool="hermes"; tbl_reason="qwen3" ;;
  *qwen2*|*qwen*)
    family="qwen"; tbl_tool="hermes"; tbl_reason="" ;;
  *deepseek*r1*)
    family="deepseek-r1"; tbl_tool="deepseek_v3"; tbl_reason="deepseek_r1" ;;
  *deepseek*)
    family="deepseek"; tbl_tool="deepseek_v3"; tbl_reason="" ;;
  *llama-4*|*llama4*)
    family="llama4"; tbl_tool="llama4_pythonic"; tbl_reason="" ;;
  *llama-3*|*llama3*)
    family="llama3"; tbl_tool="llama3_json"; tbl_reason="" ;;
  *mistral*|*magistral*|*devstral*)
    family="mistral"; tbl_tool="mistral"; tbl_reason="" ;;
  *gpt-oss*|*gpt_oss*)
    family="gpt-oss"; tbl_tool="openai"; tbl_reason="openai_gptoss" ;;
  *glm-4*|*glm4*)
    family="glm4"; tbl_tool="glm45"; tbl_reason="glm45" ;;
  *hermes*)
    family="hermes"; tbl_tool="hermes"; tbl_reason="" ;;
esac

# Explicit env wins over the table. `${VAR+x}` distinguishes "set to empty"
# (= deliberately pass no flag) from "unset" (= fall back to the table).
TOOL_PARSER="${AVA_TOOL_PARSER+x}"
if [ -n "$TOOL_PARSER" ]; then TOOL_PARSER="$AVA_TOOL_PARSER"; else TOOL_PARSER="$tbl_tool"; fi
REASON_PARSER="${AVA_REASONING_PARSER+x}"
if [ -n "$REASON_PARSER" ]; then REASON_PARSER="$AVA_REASONING_PARSER"; else REASON_PARSER="$tbl_reason"; fi
EXTRA_FLAGS="${AVA_SERVE_EXTRA_FLAGS-$tbl_extra}"

if [ -z "$family" ] && [ -z "${AVA_TOOL_PARSER+x}" ]; then
  echo "[local-serve] WARNING: no vLLM parser profile for '$MODEL'."
  echo "[local-serve]   Serving WITHOUT tool-calling rather than guessing a parser —"
  echo "[local-serve]   a wrong --tool-call-parser returns no tool_calls silently and"
  echo "[local-serve]   Ava's turns would loop until they time out."
  echo "[local-serve]   Fix: check the model card's vLLM section, then set e.g."
  echo "[local-serve]     AVA_TOOL_PARSER=hermes AVA_REASONING_PARSER= bash deploy/local-serve.sh"
  echo "[local-serve]   and set 'tools: none' on this backend in ava.yaml until you do."
fi

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
# what note-keeper's timeshare.free_super() coordinator is for (it pauses this
# container, ref-counted + debounced, so a burst of renders costs one reload).
# 0.40 does leave room for the GPU model previews (6-25 GB) and kokoro TTS to co-fit.
#
# If you serve a different model or have a dedicated GPU (no renders competing
# for the pool), raise this — 0.85-0.90 is the usual vLLM default territory.
UTIL="${AVA_SERVE_GPU_UTIL:-${OMNI_GPU_UTIL:-0.40}}"

# Stop the retired brains + any prior container (they don't co-fit).
# Skipped under AVA_SERVE_DRY_RUN so checking your flags never disturbs a live one.
if [ -z "${AVA_SERVE_DRY_RUN:-}" ]; then
  for c in vllm-super vllm-nano "$NAME"; do
    docker rm -f "$c" 2>/dev/null || true
  done
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
  printf '  docker run -d --gpus all --name %s %s\n' "$NAME" "$IMAGE"
  printf '    %s\n' "$@"
  exit 0
fi

docker run -d --gpus all --shm-size=32g \
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
