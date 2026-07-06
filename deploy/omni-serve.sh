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
# The container uses --restart unless-stopped, so it comes back on Docker/host
# restart. For a hardened always-on setup, point the host vLLM systemd unit at
# this script (see docs/OMNI_SWITCHOVER.md) instead of the old start-vllm.sh flow.
#
# ── Memory budget (121 GB unified) ────────────────────────────────────────────
#   weights (FP8)         ~35 GB      (half of BF16 — that's why we use FP8)
#   KV cache @ 32k        ~10-14 GB   (grows with --max-model-len)
#   activations/overhead   ~4-6 GB
#   => --gpu-memory-utilization 0.55 (~67 GB for vLLM), leaving ~50 GB for
#      the GPU service renders + system. Plenty of headroom at FP8.
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

IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.20.0-aarch64-cu130-ubuntu2404}"
# Hugging Face cache the container mounts. Defaults to the model store under
# AVA_HOME (matching `ava setup`); override with HF_CACHE or AVA_HOME in .env.
HF_CACHE="${HF_CACHE:-${AVA_HOME:-$REPO}/models/hf}"
MODEL="${AVA_OMNI_MODEL:-nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8}"
PORT="${OMNI_PORT:-8002}"
UTIL="${OMNI_GPU_UTIL:-0.55}"
CTX="${OMNI_MAX_LEN:-65536}"   # must exceed the agent's system-context (~29k tokens of persona + tool schemas)

# Stop the retired brains + any prior omni container (they don't co-fit).
for c in vllm-super vllm-nano vllm-open; do
  docker rm -f "$c" 2>/dev/null || true
done

echo "[omni-serve] Starting vllm-open ($MODEL, FP8, util $UTIL, ctx $CTX, eager) on :$PORT ..."
# NOTES:
#  - vLLM auto-detects the FP8 quantization from the checkpoint's config; no
#    --quantization flag needed. If a vLLM version complains, add:
#    --quantization modelopt
#  - --tool-call-parser / --reasoning-parser names are VERIFIED at cutover. If
#    vLLM rejects them, drop those two flags (+ --enable-auto-tool-choice) —
#    plain chat still works; reasoning then appears inline in the content.
docker run -d --gpus all --shm-size=32g \
  --restart unless-stopped \
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
  --tool-call-parser hermes \
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
