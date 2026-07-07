# Changelog

All notable changes to Ava are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This is a single-host
on-prem project, so versions are dated milestones rather than published releases.

## [Unreleased]

### Added — Publish-readiness (fork-portability pass)
- **Inference provider layer** (`ava_bridge/router_app.py`): the router is now an
  importable app factory with per-backend `engine` (vLLM/Ollama/llama.cpp/cloud)
  and `tools` flags, minimal engine adapters (vLLM reasoning kwargs, stream-usage
  injection, tool-capability routing), and **embeds in the bridge** at startup
  (`router_host.py`) — auto-detecting a standalone `ava-router` unit. Bare metal
  and Docker now ship the same bridge→router→engine product.
- **Router auth hardening**: `/v1/*` requires a bearer/`X-Ava-Router-Token` when
  bound off-loopback; loopback default stays open. Token in `secrets/router_token`.
- **First-run web wizard** (`/setup/wizard`): hardware+tier → backend → features
  → connectors, written to `ava.yaml` via `settings.save_patch`.
- **Connector capabilities** (generic, manifest-driven): `chat_pickup` (post-turn
  artifact quick-cards), `jobs` (GPU-attribution job polling), `model_hints`
  (loaded-model roles) — so app-specific chat/dashboard behavior is declared in a
  `connector.yaml`, not wired into core.
- **CI** (`.github/workflows/ci.yml`): ruff, pytest, frontend dist-drift, CPU-only
  smoke boot, gitleaks. New `ruff.toml`, `requirements-dev.txt`,
  `requirements-voice.txt`.
- **Security-surface tests** (42 → 108): router proxy/failover/auth, auth
  middleware + login throttle, SSRF guard (per-hop redirect revalidation),
  connector registry parsing.
- `bin/ava` is now tracked; `.env` is auto-loaded by the app itself (not just the
  run scripts); generic `AVA_SMTP_*` env (legacy `OUTLOOK_*` still accepted);
  timezone-truthful digest timestamps; frontend bundle code-split (React chunk).

### Changed — De-personalization
- First-party personal apps fully decoupled from tracked core (moved behind the
  connector manifest + overlay); default the GPU model checkpoint is now `gpu_model_base`;
  owner-specific component/architecture docs moved to the gitignored `docs/dev/`;
  `CONTRIBUTING.md` rewritten for fork contributors; a voice-dep import guard so
  a fresh install boots without `requirements-voice.txt`.

### Added — Governance & security documentation
- `SECURITY.md` (trust boundaries, egress model, secret inventory, threat model),
  `CONTRIBUTING.md`, and an Architecture Decision Record set under
  `agent/docs/adr/`.

## [2026-07-06] — Productization: pluggable apps, agent runtime & Omni switchover

A large step toward "fork Ava, connect your own apps/hardware/models, run in
minutes." Four coherent work streams:

### Added — Connector / App SDK (data-driven app surface)
- **`ui:` manifest block + `/api/apps`**: the left rail/nav is now **data-driven**
  from the connector registry. A third-party app appears by dropping a
  `connector.yaml` folder into `$AVA_HOME/connectors/` — **no React/Python edits**.
- **Three embed tiers**: `native` (first-party React view), `iframe` (the app's own
  web UI, reverse-proxied **same-origin** under `/apps/<id>/` so it inherits the
  session cookie), `none` (generic action console).
- **Generic bridge infra**: `/apps/<id>/api/*` (token-injecting browser data-proxy),
  `/apps/<id>/*` (same-origin iframe proxy), and **dynamic tool discovery**
  (`actions.discover` → `__tools`/`__call`) so an app's whole MCP-style tool set
  bridges from a manifest.
- **`examples/hello-app/`** (a runnable third-party connector) and
  **`docs/CONNECTOR_SDK.md`**; `ava connector apps` CLI.

### Added — Fork-readiness & BYO
- **First-run web setup** (`/setup`): a fresh install prompts to create the admin
  password instead of a dead login wall.
- **Degraded chat fallback**: when no agent runtime is present, chat routes
  directly to any OpenAI-compatible endpoint (a working, tool-less assistant) —
  a fresh fork works day one instead of erroring.
- **UI dynamism**: header **model switcher** (renders the user's configured
  backends), **`/api/brand`** (re-brand name/tagline via `ava.yaml`, zero code
  edits), device-honest hardware labels (de-DGX'd), and `ava doctor` **model-tier
  recommendation** from detected memory.

### Added — Pluggable agent runtime
- **`ava_bridge/runtime/`**: an `AgentRuntime` interface with `NemoClawRuntime`
  (default) and `DirectRuntime` (fallback); `agent.py` is now a thin facade.
  NemoClaw (NVIDIA, Apache-2.0 — OpenClaw-in-OpenShell) is the first-class runtime.
- **`agent.required` gate** + `ava agent provision|status` + install.sh bootstrap +
  **`docs/AGENT_RUNTIME.md`**. Selection: configured runtime if available, else the
  Direct floor; `required: true` makes the full runtime a hard requirement.

### Changed
- **Config is now driven by `ava.yaml`** for the running bridge (`config.py` layers
  env → `ava.yaml` → defaults via `settings`); `serve.py` binds host/port from it.
- **First-party apps fully migrated onto the generic connector proxy** — bespoke
  per-app routes removed; egress is auto-generated per connector
  (`agent/policies/generated/<app>.yaml`). The drift-check now
  recognizes generated connector policies.
- **De-personalized** for forks: `.env.example` sanitized (no personal paths),
  `config.PROJECTS` is dynamic (only apps whose checkout exists), report email +
  the GPU service model paths are config-driven, and dashboard/perf fallbacks are core-only.

### Fixed — Omni agent switchover
- **Ava's agent now runs on open-model 30B** (Super-120B fully retired). The sandbox
  agent had its own inference config (`~/.openclaw/openclaw.json`) still on Super;
  repointed it (+ `models.json`, host `~/.nemoclaw/*`) to Omni. See
  the Omni switchover runbook (deployment-local, `docs/dev/`).
- **`vllm-open` served at 65536 context** (was 32768) — the agent's ~29k-token
  system context now fits; `deploy/omni-serve.sh` default bumped.

### Security
- **`vllm-open` bound to `127.0.0.1` only** (was `0.0.0.0`) — inference is no longer
  exposed on external interfaces; the sandbox reaches it via the host-side guard
  proxy. `ava_security_check.py` passes.

## [2026-07-03] — Central model hub

### Changed
- **All model weights consolidated into a single machine-wide hub** (see
  `paths.models` / `AVA_MODELS_DIR`), catalogued by a registry file. Duplicated
  weights removed (verified byte-identical before deletion).
  - vLLM loads via `HF_HOME` inside the hub; the GPU service reads via
    `gpusvc/extra_model_paths.yaml` `base_path`.
  - Voice models (Piper, faster-whisper, ECAPA) live under the hub; only the
    biometric `voiceprint.npy` stays app-local.
  - The Ollama store is consolidated under the hub's caches.

### Added
- **Model Hub** node in the system diagram (engines → "load weights" → the
  hub), regenerated from the manifest.

## [2026-06-28] — Self-maintaining architecture pipeline

### Added
- **SSOT pipeline:** `agent/docs/architecture.yaml` as the single source of truth;
  `agent/docs/arch.py` generates the system & network diagrams and the README §7
  services table, and drift-checks the manifest against the running system.
- Five `architecture` MCP tools (`get_architecture`, `describe_component`,
  `check_drift`, `sync_diagrams`, `update_architecture`) so Ava can read and update
  her own architecture, gated by the `ava-knowledge` policy.
- Automation: `ava-arch-sync.path`/`.service` watcher + git pre-commit drift gate.
- Comprehensive docs: `README.md`, `agent/docs/README.md` (deployment-specific
  component notes live outside the public repo).

### Security
- App password gate on `:8445` (HMAC-signed session cookie, per-IP login throttle).

## [2026-06-26] — Voice, phone bridge, and GPU workloads

### Added
- Voice loop (`voice_ava.py`): Whisper STT → local vLLM → Piper TTS, with an
  ECAPA-TDNN speaker gate (your-voice-only).
- Phone voice/chat bridge (`phone_bridge.py`) served over Tailscale at `:8445`.
- GPU workloads via an Ava-owned the GPU service (`:8189`, the GPU model) callable by voice/text.
- Native MCP `get_weather` tool (Open-Meteo) under the narrow `ava-weather` policy.
- Routed chat through the OpenClaw agent (`main`) instead of raw vLLM.
