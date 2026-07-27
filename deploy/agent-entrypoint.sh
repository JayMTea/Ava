#!/usr/bin/env bash
# Agent-runtime container entrypoint: wait for the bridge, onboard the NemoClaw
# sandbox non-interactively (env-driven — no prompts), deploy Ava's tools, then
# serve the RemoteRuntime shim. Idempotent: a restart re-verifies and re-serves.
set -uo pipefail

: "${AVA_HOME:=/data}"
export PATH="${HOME:-/root}/.local/bin:${HOME:-/root}/.npm-global/bin:${PATH}"
ROUTER_URL="${AVA_ROUTER_URL:-http://ava:8010}"        # the bridge's embedded router
BRIDGE_URL="${AVA_BRIDGE_URL:-http://ava:8096}"         # bridge /internal callbacks
SANDBOX="${AVA_OC_SANDBOX:-my-assistant}"
AGENT="${AVA_OC_AGENT:-main}"
MODEL="${AVA_BACKEND_MODEL:-nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8}"

# --- Install the NemoClaw CLI (official installer) ---------------------------
# Done at runtime because the installer needs a reachable Docker daemon (the
# socket is mounted now). Idempotent + fast once installed; persists if
# /root is on a volume (see compose). The npm `nemoclaw` package is a stub, so
# this is the ONLY correct install path.
if ! command -v nemoclaw >/dev/null 2>&1; then
  echo "[agent] installing the NemoClaw CLI (official installer) ..."
  export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
  # The installer also attempts an onboard at the end; that step is
  # network-dependent and we do our OWN controlled onboard below, so tolerate a
  # non-zero exit and verify success by the CLI being present + runnable.
  # NEMOCLAW_INSTALL_REF is pinned by deploy/agent.Dockerfile. The `:-main`
  # fallback stays for anyone running this script outside that image, but a
  # build should never rely on it: `main` is a moving branch piped into bash.
  echo "[agent]   ref: ${NEMOCLAW_INSTALL_REF:-main}"
  curl -fsSL "https://raw.githubusercontent.com/NVIDIA/NemoClaw/${NEMOCLAW_INSTALL_REF:-main}/install.sh" | bash || true
  hash -r
  if ! command -v nemoclaw >/dev/null 2>&1; then
    echo "[agent] ERROR: NemoClaw CLI did not install — see logs above" >&2
    exit 1
  fi
fi
echo "[agent] nemoclaw: $(nemoclaw --version 2>&1 | head -1)"

echo "[agent] waiting for the bridge router at ${ROUTER_URL}/healthz ..."
for _ in $(seq 1 60); do
  curl -fsS "${ROUTER_URL}/healthz" >/dev/null 2>&1 && break
  sleep 3
done

# --- Onboard the sandbox (only if it doesn't exist yet) ----------------------
# `nemoclaw onboard` reads the whole config from NEMOCLAW_* env in non-interactive
# mode (verified against the CLI's env contract). OpenShell creates the sandbox
# container on the mounted host Docker daemon.
if ! nemoclaw list --json 2>/dev/null | grep -q "\"${SANDBOX}\""; then
  echo "[agent] onboarding sandbox '${SANDBOX}' (non-interactive) ..."
  export NEMOCLAW_NON_INTERACTIVE=1
  export NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1
  export NEMOCLAW_SANDBOX_NAME="${SANDBOX}"
  export NEMOCLAW_AGENT="${AGENT}"
  export NEMOCLAW_PROVIDER="compatible-endpoint"        # OpenAI-compatible
  export NEMOCLAW_INFERENCE_BASE_URL="${ROUTER_URL}/v1"
  export NEMOCLAW_INFERENCE_API="openai-completions"
  export NEMOCLAW_MODEL="${MODEL}"
  # Bearer for the router's /v1. In Docker the router binds 0.0.0.0 (so ava:8010
  # is reachable), which turns /v1 auth ON — the sandbox must present the token.
  # Both containers derive it from the shared /data secret, so resolve it here.
  ROUTER_TOKEN="${AVA_ROUTER_TOKEN:-$(python -c 'from ava_bridge import config; print(config.ROUTER_TOKEN or "")' 2>/dev/null)}"
  export OPENAI_API_KEY="${ROUTER_TOKEN:-none}"
  if ! nemoclaw onboard --non-interactive --yes \
        --yes-i-accept-third-party-software --name "${SANDBOX}"; then
    echo "[agent] ERROR: onboarding failed — see logs above" >&2
    exit 1
  fi
  # The sandbox reaches the bridge at host.openshell.internal; point that alias
  # at the bridge container's address so /internal/* callbacks + the router
  # resolve. (Host-dependent — override with AVA_BRIDGE_IP if autodetect misses.)
  BRIDGE_IP="${AVA_BRIDGE_IP:-$(getent hosts ava 2>/dev/null | awk '{print $1; exit}')}"
  if [ -n "${BRIDGE_IP:-}" ]; then
    echo "[agent] mapping host.openshell.internal -> ${BRIDGE_IP}"
    nemoclaw "${SANDBOX}" hosts-add host.openshell.internal "${BRIDGE_IP}" || true
  else
    echo "[agent] WARNING: could not resolve the bridge IP; set AVA_BRIDGE_IP so the" \
         "sandbox can reach the bridge (see deploy/README.md)." >&2
  fi
fi

# --- Deploy Ava's tools/policies/skills into the sandbox (idempotent) --------
echo "[agent] deploying tools/policies/skills ..."
AVA_BRIDGE_URL="${BRIDGE_URL}" AVA_OC_SANDBOX="${SANDBOX}" \
  bash /app/agent/install.sh || echo "[agent] WARNING: install.sh reported issues"

echo "[agent] serving the runtime shim on :9100 ..."
exec python -m ava_bridge.agent_runtime_server
