#!/usr/bin/env bash
# omni-serve.sh — start Ava's single ALWAYS-ON brain: Nemotron open-model 30B-A3B (FP8).
#
# Ava keeps ONE model resident at all times (no scaling models up/down). This
# script (re)starts just the vllm-open container on :8002. It replaces the old
# vllm-super (120B) + vllm-nano (8B) containers, which cannot co-fit alongside
# the Omni model in the GB10's 121 GB unified pool.
#
# ── One-time / after reboot ───────────────────────────────────────────────────
#   bash deploy/omni-serve.sh
#
# The container restarts on Docker/host restart by default (OMNI_RESTART). If Ava's
# allocator manages this container, set OMNI_RESTART=no so there is only one supervisor
# — see the note by RESTART below.
#
# NOTE: with OMNI_RESTART=no, nothing brings the container back after a reboot (the
# allocator restores only what it released itself). Pair it with a boot unit that runs
# THIS script — Type=oneshot + RemainAfterExit, so systemd starts it and then stops
# caring, and will not fight the allocator when it stops the container for a render.
# `ava-omni.service` on the development box is exactly that.
#
# ── Memory budget (121.7 GB unified) ──────────────────────────────────────────
#   weights (FP8)         ~35 GB      (half of BF16 — that's why we use FP8)
#   KV cache @ 32k        ~10-14 GB   (grows with --max-model-len)
#   activations/overhead   ~4-6 GB
#
# CORRECTED 2026-07-25 — this block used to claim 0.55 (~67 GB) left "~50 GB for
# the GPU service renders + system. Plenty of headroom." Both halves were wrong, and the
# error cost 7997 container restarts. MEASURED on this host:
#
#   total unified pool                        121.7 GiB
#   OS + the other containers (baseline)      ~33   GiB   <- NOT ~19
#   vLLM at util 0.55                          66.9 GiB
#   ------------------------------------------------------
#   MemAvailable with omni up                 ~34   GiB   <- measured, not ~50
#
# READING THE NUMBERS: use MemAvailable from /proc/meminfo, NOT the GPU service's
# /system_stats vram_free. On this unified-memory host vram_free tracks MemFree
# and so ignores ~30 GiB of reclaimable page cache (reading FLUX.2's 66 GiB of
# weights fills it), which makes it read 5-9 GiB when 34 GiB is genuinely
# available. Both numbers move for unrelated reasons; MemAvailable is the honest
# one, and neither is comparable across a render unless you drop caches.
#
# The "~50 GB is plenty" claim assumed an image render costs 6-25 GB, which was
# true for the GPU model / FLUX.1. note-keeper went FLUX.2-only on 2026-07-22 and
# FLUX.2 is 66.5 GiB RESIDENT (33.0 UNET + 33.1 mistral text encoder + 0.3 VAE),
# so a render and this model CANNOT cofit at any --gpu-memory-utilization. When a
# render held that memory, vLLM's startup check failed ("Free memory on device
# cuda:0 (33.95/121.69 GiB) ... less than desired ... (0.55, 66.93 GiB)") and
# --restart unless-stopped retried every ~20s forever.
#
# Two consequences, both handled:
#   * FLUX.2 renders now PAUSE this container and hand the memory back, via
#     note-keeper's timeshare.free_super() coordinator (ref-counted +
#     debounced, so a burst of renders costs one reload, ~30s off page cache).
#   * util is capped at 0.40 (~48.7 GiB) below, which hands ~18 GiB back to the
#     pool vs 0.55. That is enough for the GPU model previews (6-25 GB) and kokoro TTS to
#     cofit; it is NOT enough for FLUX.2, which still needs the pause. Verified
#     2026-07-25: 0.40 boots and serves the full 65k ctx.
#
# Sizing KV is about CONCURRENCY, not context: vLLM needs only >=1 sequence's
# worth of KV to accept --max-model-len, and it reported 114x concurrency at
# 0.55. The old "KV cache @ 32k ~10-14 GB" line above reads like a floor for the
# context; it is not. Cutting CTX is the wrong lever here anyway (see its note).
#
# ── IMPORTANT: --enforce-eager ────────────────────────────────────────────────
# This omni model captures CUDA graphs across 512 batch sizes INCLUDING its
# vision encoder; that transient spike OOM-killed the engine on the 121 GB pool.
# --enforce-eager disables graph capture (small runtime perf cost, big memory
# saving + faster init) and is what makes the always-on load fit. Remove it only
# if you have proven headroom and want the ~5-10% decode speedup.
set -uo pipefail

# Resolve the repo root and load .env (HF_CACHE, AVA_HOME, ports …) if present,
# so this script has no hardcoded personal paths.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/.env" ] && set -a && . "$REPO/.env" && set +a

# Default image is aarch64 (DGX/GB10-class hosts). On x86_64 override it:
#   VLLM_IMAGE=vllm/vllm-openai:latest ./deploy/omni-serve.sh
IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.20.0-aarch64-cu130-ubuntu2404}"
# Hugging Face cache the container mounts. Defaults to the model store under
# AVA_HOME (matching `ava setup`); override with HF_CACHE or AVA_HOME in .env.
HF_CACHE="${HF_CACHE:-${AVA_HOME:-$REPO}/models/hf}"
MODEL="${AVA_OMNI_MODEL:-nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8}"
PORT="${OMNI_PORT:-8002}"
# 0.40 (~48.7 GiB), lowered from 0.55 on 2026-07-25. vLLM's own startup log at
# 0.55 reported "Available KV cache memory: 27.14 GiB / GPU KV cache size:
# 1,896,048 tokens / Maximum concurrency for 65,536 tokens per request: 114.31x"
# — 114 concurrent full-context requests for a single-user assistant. Weights +
# activations measure ~39.8 GiB of that budget. MEASURED at 0.40: KV 9.7 GiB /
# 676,704 tokens / 40.82x concurrency at the full 65k ctx — still ample — while
# handing ~18 GiB back to the pool. Do NOT trade CTX for headroom instead.
UTIL="${OMNI_GPU_UTIL:-0.40}"
# Docker's restart policy vs the allocator: they are both supervisors, and only one
# can decide when there is room. `unless-stopped` (the default here, right for a box
# with no allocator) retries a start forever with no backoff — the mechanism behind the
# 7997 restarts above. If you declare this container in ava.yaml `alloc.models`, set
# OMNI_RESTART=no so Ava is the sole supervisor; `ava doctor` flags the combination.
RESTART="${OMNI_RESTART:-unless-stopped}"
CTX="${OMNI_MAX_LEN:-65536}"   # must exceed the agent's system-context (~29k tokens of persona + tool schemas)

# Stop the retired brains + any prior omni container (they don't co-fit).
for c in vllm-super vllm-nano vllm-open; do
  docker rm -f "$c" 2>/dev/null || true
done

echo "[omni-serve] Starting vllm-open ($MODEL, FP8, util $UTIL, ctx $CTX, eager, restart=$RESTART) on :$PORT ..."
# NOTES:
#  - vLLM auto-detects the FP8 quantization from the checkpoint's config; no
#    --quantization flag needed. If a vLLM version complains, add:
#    --quantization modelopt
#  - --tool-call-parser / --reasoning-parser names come straight from the model
#    card's vLLM section. Omni emits tool calls in the qwen3_coder XML format
#    (<function=name><parameter=x>…). Using the wrong parser (e.g. `hermes`,
#    which expects JSON) makes vLLM return NO tool_calls — the call falls through
#    as plain text, the agent never sees it, and turns loop until they time out
#    (looks like Ava "not responding"). If vLLM ever rejects these names, check
#    the model card, don't just drop them.
docker run -d --gpus all --shm-size=32g \
  --restart "$RESTART" \
  -v "${HF_CACHE}:/root/.cache/huggingface" \
  -p "${OMNI_BIND:-127.0.0.1}:${PORT}:8000" \
  --name vllm-open \
  "${IMAGE}" \
  --model "${MODEL}" \
  --trust-remote-code \
  --dtype auto \
  --max-model-len "${CTX}" \
  --gpu-memory-utilization "${UTIL}" \
  --enforce-eager \
  --max-num-batched-tokens 4096 \
  --enable-prefix-caching \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser nemotron_v3

echo "[omni-serve] Waiting for readiness on :$PORT (bounded ~15 min) ..."
deadline=$(( $(date +%s) + 900 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    echo "[omni-serve] vllm-open READY → http://localhost:${PORT}/v1 ($MODEL)"
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep vllm || true
    exit 0
  fi
  st=$(docker inspect -f '{{.State.Status}}' vllm-open 2>/dev/null || echo gone)
  if [ "$st" != running ]; then
    echo "[omni-serve] ERROR: container is '$st' — last logs:"
    docker logs --tail 30 vllm-open 2>&1 | tail -30
    exit 1
  fi
  docker logs vllm-open 2>&1 | grep -E "shards|Application startup|ERROR|out of memory" | tail -1 || true
  sleep 6
done
echo "[omni-serve] TIMEOUT waiting for readiness; check: docker logs vllm-open"
exit 1
