# Ava — Component & Location Reference

Detailed, file-by-file map of every moving part. For the high-level picture and
flow diagrams, start at the root [README.md](../../README.md). For design
philosophy and roadmap, see [ARCHITECTURE.md](ARCHITECTURE.md).

> **🔑 Single Agent, Two Front Doors**  
> — The `:8445` app (phone experience) and `:8444` dashboard (admin control)
> both talk to the **same OpenClaw agent `main`**. Memory, tools, skills,
> approvals, and learning state are **shared across both**. Any action in one
> UI is visible in the other. They're not two assistants — they're one assistant
> with different UI surfaces.

---

## A. Processes & where they run

| Process | Runs as | Lives on disk | Listens | Notes |
|---------|---------|---------------|---------|-------|
| **ava-bridge** (`phone_bridge.py`) | `ava-bridge.service` (user) | `~/projects/Ava` | `127.0.0.1:8096` → TS `:8445` | Thin FastAPI face over the `ava_bridge/` package: chat UI, push-to-talk, image gen, doc read-back. Loads Whisper + ECAPA at startup (~25 s cold). |
| **ava-bridge-gw** (`ava_bridge/gw_forward.py`) | `ava-bridge-gw.service` (user) | `~/projects/Ava` | `172.27.0.1:8096` (sandbox docker-gateway, dynamic) | Forwards the sandbox-gateway `:8096` → `127.0.0.1:8096` so Ava's MCP tools can reach `/internal/*`. Sandbox-only (not on wifi/tailnet); separate unit so a rebuild never downs the core bridge. |
| **ava-gpusvc** (the GPU service) | `ava-gpu.service` + `ava-gpusvc-gw.service` (user) | `~/the GPU service` + `~/projects/Ava/gpusvc` | `127.0.0.1:8189` + sandbox-gateway `:8189` | Ava's *own* the GPU service (separate from any other). the GPU model `lustifygpumodelNSFW_apexV8`. Reads all weights from the central **`~/ai/models/latent pipeline`** hub via `gpusvc/extra_model_paths.yaml`. Cold start ~60 s. |
| **your connectors** (optional) | e.g. `<app>.service` (user) | `~/projects/<app>` | `127.0.0.1:<port>` | Apps Ava monitors/drives, registered as connectors (`connectors/<id>/connector.yaml`) and reached via the generic connector proxy. First-party/personal apps live in the gitignored overlay; the public core ships none. |
| **ava-web** (`searxng/run.sh`) | `ava-web.service` (user, oneshot) | `~/projects/Ava/searxng` | — | Boot unit that provisions the anonymized web-egress stack: the private `ava-web` Docker network, `ava-tor`, then `ava-searxng`. Waits for a confirmed Tor circuit + the SearXNG JSON API before exiting. Idempotent (safe to re-run). |
| **ava-tor** (Docker `osminogin/tor-simple`) | Docker container (`--restart unless-stopped`) | — | `127.0.0.1:9050` | Tor SOCKS5 proxy on a private network. Anonymizing egress for both `web_search` (via SearXNG) and `web_fetch` (via the host reader). Loopback-only; reached only by the host. |
| **ava-searxng** (Docker `searxng/searxng`) | Docker container (`--restart unless-stopped`) | `~/projects/Ava/searxng/settings.yml` | `127.0.0.1:8888` | Private SearXNG metasearch. All engine queries egress through `ava-tor` (`outgoing.proxies`, fail-closed). JSON API consumed by the host bridge only; the sandbox never touches it. |
| **OpenClaw agent `main`** | inside Docker sandbox | container `openshell-my-assistant-…` | — | The actual "Ava" reasoning agent. Memory, tools, skills. |
| **openshell-gateway** | host process | `~/.local/bin/openshell-gateway` | `127.0.0.1:8080`, `172.27.0.1:8080` | Agent ↔ host plumbing. |
| **OpenClaw dashboard** | inside sandbox, SSH-forwarded | — | `127.0.0.1:18789` → TS `:8444` | Admin/control plane UI. |
| **vLLM** | Docker container (`vllm-open`) | local Docker runtime | `127.0.0.1:8002` | Local LLM serving behind `ava-router`. **Nemotron open-model 30B-A3B (FP8)** — Ava's single **always-on** conversational + multimodal brain (kept resident; no scaling models up/down). Loads weights from the central **`~/ai/models/_hf`** hub (`HF_HOME`); `~/.cache/huggingface` is a symlink into it. |
| **ava-router** (`ava_router.py`) | `ava-router.service` + `ava-router-gw.service` (user) | `~/projects/Ava` | `127.0.0.1:8010` + sandbox-gateway `:8010` | OpenAI-compatible router the agent points at instead of vLLM directly. Fronts the always-on model brain for token/perf logging + the hardware-fit `GET /fit` view; supports an optional cloud fallback + workload hints (`X-Ava-Workload`) if extra backends are declared. Reads hardware via the HAL (`ava_bridge/hwinfo.py`). |
| **nemoclaw-recover** | `nemoclaw-recover.service` (user, oneshot) | `~/.local/bin/nemoclaw-boot-recover.sh` | — | Boot: start gateway + restore dashboard forward. |

---

## B. `~/projects/Ava/` — the Experience-plane app

| Path | Purpose |
|------|---------|
| `phone_bridge.py` | Thin `:8445` app — wiring + route handlers ONLY (auth, serve, capture input, forward to Ava). `/api/talk` (voice), `/api/talk-text` (chat), `/api/generate` + `/api/job` (image), `/api/chats*` (history), `/internal/*` (sandbox doc read-back), `/media` static. Serves the built **React SPA** (`frontend/dist/`) at `/` (mounts `/assets`); the original single-file UI is kept at **`/legacy`** during the migration. |
| `frontend/` | **Vite + React + TypeScript SPA** (the Experience UI). Static-built to `frontend/dist/` (no Node in prod). Claude.ai-style look: warm-dark theme, collapsible **icon rail** sidebar, centered chat column, stacked composer with attach + **mic** (push-to-talk `/api/talk`) + send. `src/` = `App.tsx` (shell + view switch), `hooks/useChat.ts` (turn engine: chat-stream poll, image jobs, voice), `components/` (`chat/`, `artifact/`, `learning/`, `Drawer`, `Header`), `lib/` (`api.ts` typed client, `types.ts`, `icons.tsx`), `styles/` (`global.css` base + `claude.css` theme). The tabs — **Ava — Assistant** and **Learning** (plus any connected-app views) — are native React views. Build: `cd frontend && npm run build`. |
| `ava_bridge/` | The bridge broken into one-concern modules: `config` (paths/env/INTERNAL_TOKEN), `state` (shared dicts/locks), `agent` (`ask_openclaw`), `auth` (password gate + cookies), `chat_store`, `gpu_jobs`, `turns`, `documents` (PDF/Office/OCR extraction), `audio`, `internal` (token-gated `/internal` payloads), `web` (private SearXNG search + Tor-routed, SSRF-guarded page reader), `gw_forward` (sandbox-gateway forwarder), **`hwinfo`** (hardware abstraction layer — see §B.HAL), **`model_fit`** (workload/memory fit engine behind the router), **`hardware`** (Command Center telemetry, via the HAL), `web/` (the legacy single-file UI + `login.html`, served at `/legacy` and `/login`). |
| `ava_router.py` | OpenAI-compatible **inference router** (`ava-router.service`, `:8010`) — see §A. Fronts Ava's always-on open-model brain (single backend); optional workload-fit selection + cloud failover if you declare extra backends; `GET /fit` exposes live fit state. |
| `voice_ava.py` | Standalone terminal voice loop (mic VAD → Whisper → agent → Piper). Modes `listen` / `ptt`. |
| `speaker.py` | ECAPA-TDNN speaker verification; `cosine()`, `SpeakerVerifier`, `load_voiceprint()`. |
| `enroll_voice.py`, `enroll_from_file.py` | Build `models/voiceprint.npy` from live mic or an audio file. |
| `gpu_service.py` | Minimal the GPU service client: builds the the GPU model graph, queues `/prompt`, tracks progress, saves PNG to `media/out`. |
| `run.sh`, `run_bridge.sh`, `devices.sh` | Local run + Tailscale-serve + ALSA device helpers. |
| `.venv/` | Shared Python venv (faster-whisper, torch CPU, fastapi/uvicorn, speechbrain, etc.). |
| `bin/piper/` | Prebuilt Piper TTS binary (aarch64). |
| `models/` | Symlinks into the central **`~/ai/models` hub**: `en_US-amy-medium.onnx` (+ `.json`), `en_US-ryan-medium.onnx` → `audio/tts/`; `ecapa/` → `audio/speaker/`. Only `voiceprint.npy` (biometric) is a real local file. |
| `media/out`, `media/uploads` | Generated images, user uploads. |
| `data/chats.json` | Chat history persistence. |
| `gpusvc/` | `extra_model_paths.yaml` (`base_path: ~/ai/models/latent pipeline` — the central hub) + `user/` for `ava-gpusvc`. |
| `enroll/` | Enrollment script + recordings. |
| `agent/` | **Admin config kit** (see §D). |
| `openclaw/` | Apply script + legacy config (see §D). |

### B.HAL — Hardware abstraction layer & the fit router

The one place the app reads hardware. Hardware reads used to be duplicated (`nvidia-smi` + `/proc`) and Linux-only; the HAL centralises them behind a small, native, cross-platform surface consumed by **both** the router and the dashboard — so the same code works on the DGX Spark GB10 (unified memory), a discrete-NVIDIA box (VRAM), an Apple Silicon Mac mini (unified memory, no `nvidia-smi`), and CPU-only/cloud hosts.

On this deployment Ava runs a **single always-on model** (open-model 30B), so the router mainly does perf logging + the `/fit` view and there's nothing to select between. The fit **engine** below is still general — it powers workload→model selection and memory-pressure shedding for forks that declare multiple backends (e.g. the Mac two-model example in `config.example.yaml`) or an optional cloud fallback.

| Component | File | Purpose |
|-----------|------|---------|
| **HAL** | `ava_bridge/hwinfo.py` | Native, platform-adaptive hardware probing. `fit_memory()` picks the fit-limiting pool (free VRAM on a discrete GPU, else unified/system RAM); `gpus()` returns per-accelerator telemetry. Readers: **psutil** (system/unified RAM — macOS/Linux/Windows), **NVML** via `nvidia-ml-py` → `nvidia-smi` fallback (NVIDIA VRAM + util/temp/power), Apple Silicon partial (memory yes; util/temp/power `None` — no unprivileged API). Anything unreadable → `None` (never faked). `snapshot()` dumps it all for `/fit` + `ava doctor`. Adding an accelerator = one provider here, nothing else changes. |
| **Fit engine** | `ava_bridge/model_fit.py` | Decides routing from the HAL signal: maps **workload → model tier**, keeps `min_free_gb` headroom (sheds the big model under memory pressure), and **never memory-gates remote/cloud backends**. Fit profiles are declared per backend in `ava.yaml` `inference.backends.<id>.fit` (`weight_gb` / `tier` / `workloads` / `min_free_gb` / optional `local`). |
| **Fit router** | `ava_router.py` | OpenAI-compatible proxy (`:8010`). Honours an `X-Ava-Workload` header (or `workload` body field), orders backends by fit + workload via `model_fit`, then error-fails-over. `GET /fit` reports live fit state (`gating: enabled\|disabled`, `mem_source`, `platform`, per-backend fit). |
| **Monitor** | `ava_bridge/hardware.py` | Command Center telemetry — GPU/CPU/memory via the HAL + loaded-model/job discovery + time-series sampler. |

Verified on the GB10 on-device; Apple Silicon / CPU-only / no-psutil paths are simulation-tested (`tests/test_hwinfo.py`, `tests/test_model_fit.py`). On-device Mac checklist: **`docs/HWINFO_VALIDATION.md`**. Config schema (incl. Apple/Ollama example): `config.example.yaml` `inference:`.

### B.1 `frontend/` — the React SPA (Experience UI)

Vite + React + TypeScript, static-built to `frontend/dist/` and served by the
bridge at `/`. Claude.ai-style: warm-dark theme, an **icon rail** sidebar (new
chat + one icon per page), centered chat column, plain-text assistant replies,
right-aligned user bubbles, and a stacked composer (attach · mic · send).

| Path | Purpose |
|------|---------|
| `src/App.tsx` | Root shell: icon-rail `Drawer` + `Header` + view switch (`chat` / `learning` / app views) + `Composer` + artifact panel. |
| `src/hooks/useChat.ts` | The chat turn engine: `/api/chat-stream` start + `/api/turn/{id}` polling, chain-of-thought, image-job polling, **voice** (`talk()` → `/api/talk`, plays Ava's WAV reply), history, chat list. |
| `src/components/chat/` | `ChatView`, `Message`, `Composer` (mic push-to-talk), `ChainOfThought`, `Media` (lightbox, image gen, previews). |
| `src/components/artifact/` | `ArtifactPanel` + `WeatherArtifact` (Claude-style split side panel). |
| `src/components/learning/` | **Learning** view: `LearningView` (Code/Chat tabs, cycles, proposals, staged diffs, approve/reject/feedback). Talks to `/api/learning/*`. |
| `src/components/{Drawer,Header}.tsx` | Icon-rail sidebar (nav + chat history nested under the Assistant tab) and top bar. |
| `src/lib/` | `api.ts` (typed client: `api`, `learning`), `types.ts`, `icons.tsx` (inline SVGs). |
| `src/styles/` | `global.css` (base, ported from legacy) + `claude.css` (the Claude theme layer, loaded after global). |
| `dist/` | Built output the bridge serves (`index.html` + `assets/`). Rebuild: `cd frontend && npm run build`. |

> The legacy single-file UI (`ava_bridge/web/index.html`) is still served at
> **`/legacy`** as a fallback while the SPA matures.

---

## C. Connected apps (connectors)

Apps Ava monitors and drives are **connectors**, not core code: drop a
`connectors/<id>/connector.yaml` (health probe, perf-log source, egress policy,
agent actions) and Ava picks it up — dashboard health, perf sources, MCP tools,
and egress all derive from the manifest. See
[CONNECTOR_SDK.md](CONNECTOR_SDK.md). First-party / personal apps live in the
gitignored overlay; the public core ships only the `gpu-service` connector.

---

## D. Admin / agent configuration

Two directories cooperate to define the agent that runs **inside the sandbox**.
The sandbox's `openclaw.json` is **ephemeral** (rebuilt on `nemoclaw rebuild`),
so the source of truth is on the host here and pushed in.

**`agent/` — structured config kit**

| Path | Purpose |
|------|---------|
| `agent/persona.txt.tmpl` | Agent identity / system-prompt **template** (rendered from `ava.yaml` brand/owner/persona by `render_persona.py` at install — name, owner facts, anti-refusal rules). |
| `agent/mcp_server/*.mjs` | The MCP server and its tools, organized into **category subfolders**: `_server.mjs` (harness — recursively auto-loads every non-`_` module from any subfolder), `_lib.mjs` (shared HTTP/proxy helpers), `creative/` (`image`), `daily/` (`weather`, + news/markets), `health/`, `finance/`, `productivity/`, `knowledge/` (each with a README of planned tools). A tool's identity is its `name` field, not its path, so moving modules between folders never changes what Ava calls. |
| `agent/skills/` | Native skills — `ava-weather`, `ava-gpu`, `ava-web`, `ava-knowledge`, `ava-architecture`, `ava-self-coding` (`SKILL.md` each). |
| `agent/policies/` | Per-tool egress allow-lists (`ava-weather.yaml`, `ava-gpusvc.yaml`, `ava-knowledge.yaml`, …); connector policies are generated under `generated/`. |
| `agent/templates/` | Scaffolds for new tool / skill / policy. |
| `agent/install.sh`, `agent/new-tool.sh` | Deploy the kit / scaffold a new capability. `install.sh` registers `mcp.servers.ava-tools` → `mcp_server/_server.mjs` and is idempotent; re-run after `nemoclaw rebuild`. |
| `agent/snapshot.sh` | Snapshot Ava's in-sandbox agent state (memory/identity/config) via `nemoclaw <sandbox> snapshot create` and prune to the newest `AVA_SNAPSHOT_KEEP` (default 14). Run daily by `ava-snapshot.timer`. Restore with `nemoclaw <sandbox> snapshot restore`. |

> Egress note: HTTP from inside the sandbox must go through the guard proxy
> (`10.200.0.1:3128`) **with** the CA bundle (`/etc/openshell-tls/ca-bundle.pem`);
> the proxy does TLS-MITM. Tools read the proxy from a `--proxy` arg because
> gateway-spawned processes don't inherit `HTTPS_PROXY`.

**Hardening (repo root)**

| Path | Purpose |
|------|---------|
| `.gitignore` | Tracks source + `agent/` + docs; ignores `.venv/`, `models/` (incl. biometric `voiceprint.npy`), `media/`, `data/` (secrets + chats), `bin/`, and `.env`. |
| `.env.example` → `.env` | Secrets/overrides template. Copy to `.env` (gitignored, `chmod 600`); auto-loaded by `run.sh`, `run_bridge.sh`, and `ava-bridge.service` (`EnvironmentFile=`). Holds `AVA_PASSWORD`, `AVA_SECRET`, thresholds, ports, OpenClaw routing. |
| `data/auth_password`, `data/.secret` | Generated login password + cookie-signing key (`0600`, gitignored). Override via `AVA_PASSWORD` / `AVA_SECRET` in `.env`. |

---

## E. Key endpoints (quick reference)

| Endpoint | Service | Use |
|----------|---------|-----|
| `GET /login`, `POST /login`, `POST /logout` | bridge | Password session auth (signed cookie); only `/login` + `/api/health` are public |
| `POST /api/talk` | bridge | Voice turn (audio in → reply audio); routes through the OpenClaw agent |
| `POST /api/talk-text` | bridge | Text chat turn; routes through the OpenClaw agent |
| `POST /api/generate`, `GET /api/job/{id}` | bridge | Chat image gen + progress. **Sanctioned direct path** (explicit user "generate" action) that bypasses the agent and renders on local the GPU service; conversational turns must not use it |
| `GET /internal/documents`, `POST /internal/extract` | bridge | **Sandbox-tool callback surface.** Token-gated (`X-Ava-Internal-Token`), reached from the sandbox via the `ava-bridge-gw` forwarder under the `ava-knowledge` egress policy. Backs the `list_documents` / `read_document` MCP tools; never exposed to the LAN |
| `GET /internal/architecture`, `POST /internal/architecture/{describe,check,sync,update}` | bridge | **Architecture-tool callback surface.** Same token + `ava-knowledge` egress. Backs Ava's 5 `architecture` MCP tools; delegates to `agent/docs/arch.py` |
| `POST /internal/web/search`, `POST /internal/web/fetch` | bridge | **Web callback surface.** Token-gated (`ava-knowledge` egress). Backs `web_search` (private SearXNG `:8888`) and `web_fetch` (SSRF-guarded reader). Both egress via **Tor** (`ava-tor :9050`, `socks5h`), fail-closed; the sandbox never reaches the internet directly |
| `POST /internal/code-change` (body `project: ava` or a connected project) | bridge | **Code-change callback.** `code_agent` drives Claude over the target repo. `ava` = own source (auto/approval tiers); connected projects = approval-only, review branch, compile/test-gated. Backs `code_change_request` |
| `GET/POST/DELETE /api/chats*` | bridge | Chat history |
| **Learning endpoints** | | |
| `GET /api/learning/{code\|chat}/state` | bridge | Full learning state for a context: cycles → patterns + proposals (used by the Learning view) |
| `GET /api/learning/summary` | bridge | Get latest approved proposals + auto-fixes (read-only summary) |
| `POST /api/learning/{code\|chat}/apply?proposal_id` | bridge | Approve a proposal (set `status="approved"`; code proposals with staged changes are written + committed) |
| `POST /api/learning/{code\|chat}/reject?proposal_id` | bridge | Reject a proposal (set `status="rejected"`) |
| `POST /api/learning/{code\|chat}/feedback?proposal_id&rating=1\|0` | bridge | Rate approved proposal (👍/👎 feedback) |
| `GET /` (`:18789` → `:8444`) | dashboard | Admin/control |

---

## F. Architecture SSOT (self-maintaining diagrams + docs)

Ava's whole system is described by one machine-readable manifest, and the
diagrams + docs are **generated** from it — so they stay 1:1 with the code.

| Piece | Location | Role |
|-------|----------|------|
| Manifest (SSOT) | `agent/docs/architecture.yaml` | The truth: `meta`, `layers`, `zones`/`net_*`, `services`, `capabilities`, `policies`. Edit here. |
| Generator / validator | `agent/docs/arch.py` | `render` (D2 diagrams), `tables` (README §7), `sync` (both, `--commit`), `check` (drift vs reality, `--strict`), `summary`/`describe` (JSON), `update` (replace manifest, drift-gate, commit). |
| Generated diagrams | `agent/docs/diagrams/{system,network}.{d2,svg}` | Regenerated from the manifest — never hand-edit. |
| Generated README table | `README.md` §7, between `<!-- ARCH:services:start/end -->` | Services & ports table. |
| Bridge wrapper | `ava_bridge/architecture.py` | Thin subprocess wrapper over `arch.py` behind the `/internal/architecture*` routes. |
| Ava's tools | `agent/mcp_server/architecture/*.mjs` | `get_architecture`, `describe_component`, `check_drift`, `sync_diagrams`, `update_architecture`. |
| Skill | `agent/skills/ava-architecture/SKILL.md` | Tells Ava to read before answering and edit the manifest (not the SVGs). |
| Auto-sync | `~/.config/systemd/user/ava-arch-sync.{path,service}` | Path-watcher: on manifest/tool/policy change → regenerate + auto-commit. |
| Commit gate | `.git/hooks/pre-commit` | Regenerates, stages, and blocks the commit on structural drift / stale diagrams. |

**Drift gate.** `arch.py check` compares the manifest against the running system
(systemd units, listening ports, the actual MCP tool modules, policy files, and
diagram freshness). `update` reverts any edit that doesn't match reality, so the
rule is: change the **code first**, then record it in the manifest. Both the
path-watcher's commits and Ava's `update_architecture` commits are authored
`Ava (auto-sync) <ava@localhost>`.

---

## G. Learning system (code improvement + pattern analysis)

Ava's self-learning and code-editing infrastructure:

| Component | File | Purpose |
|-----------|------|---------|
| **Code learner** | `ava_bridge/learning.py` (class `CodeLearner`) | Analyzes code changes: files modified, error patterns, change acceptance rates. Proposes refactorings, caching improvements, anti-pattern fixes. |
| **Chat learner** | `ava_bridge/learning.py` (class `ChatLearner`) | Analyzes conversation history: topics, response times, tool usage, capability gaps. Proposes chat quality improvements. |
| **Code editor** | `ava_bridge/coder.py` | Claude-powered code staging: generates diffs, displays CoT reasoning, stages edits, handles inline error fixing (timeout → cache hint, large file → batch reduction, regex → simplification), commits to git. |
| **State manager** | `ava_bridge/state.py` | Thread-safe shared state: `code_learning_state` dict, `chat_learning_state` dict, `code_turns`, `chat_turns`, `turns`. Each guarded by its own `RLock`. |
| **Learning routes** | `phone_bridge.py` (`/api/learning/*`) | HTTP endpoints: `/api/learning/summary` (GET), `/api/learning/{code\|chat}/apply` (POST), `/reject`, `/feedback`. All session-gated. |
| **Digest generation** | `ava_learning_digest.py` | Daily digest: fetches learning state, generates beautiful HTML, auto-saves to `logs/learning_digest.html`, emails (optional). Triggered by `ava-learning-digest.timer` at 4am PST. |
| **Weekly trends** | `ava_learning_weekly.py` | Weekly summary: analyzes past 7 days' cycles, generates HTML with stats/trends/reflection, saves to `logs/learning_weekly_trends.html`. Triggered Sundays at 4am PST. |
| **Email tool** | `agent/mcp_server/email/{get_email_digest,list_email_files}.mjs` | MCP tools (read-only): `get_email_digest('daily'\|'weekly')` reads HTML digests, `list_email_files()` lists archives. Ava can reference her own learning via chat. |
| **Email policy** | `agent/policies/ava-email-read-only.yaml` | Read-only filesystem access: allows Ava to read `logs/learning_*.html`, blocks write/send. |
| **Learning skill** | `agent/skills/ava-email-read/SKILL.md` | Tells Ava when/how to reference her digests. |
| **Timers** | `~/.config/systemd/user/ava-learning-{digest,weekly}.timer` | Systemd path timers: trigger digest generation on schedule (daily 4am, weekly Sunday 4am PST). Persistent=true so they run even if system clock jumps. |

**Key data structures:**
- `code_learning_state`: `{last_cycle: str|None, cycles: [{patterns, proposals}], inline_fixes: [{error, fix, critical, retried}]}`
- `chat_learning_state`: Same structure, separate context
- Each `proposal`: `{id, status, requires_approval, generated_at, feedback_helpful, feedback_unhelpful, ...}`

**Approval workflow:**
1. Learner generates proposal (status="pending_approval")
2. User approves via `/api/learning/{ctx}/apply?proposal_id` (status="approved")
3. User rates via `/api/learning/{ctx}/feedback?proposal_id&rating=1` (feedback_helpful++)
4. Later cycles weight proposals by feedback

**Error recovery:**
- Non-critical errors auto-fixed with retry (timeout, large-file, regex patterns)
- Critical errors (permissions, unavailable resources) require manual intervention
- Successes logged to `inline_fixes[]` and counted in cycle analysis

---

## H. Environment knobs (bridge → agent)

| Var | Default | Meaning |
|-----|---------|---------|
| `AVA_PASSWORD` | _(generated)_ | Login password. If unset, one is generated into `data/auth_password` (0600) and logged |
| `AVA_SECRET` | _(generated)_ | HMAC key for session cookies. If unset, persisted to `data/.secret` (0600) |
| `AVA_SESSION_TTL_DAYS` | `30` | Session cookie lifetime |
| `AVA_COOKIE_SECURE` | `1` | Secure-cookie flag (set `0` only for plain-http local debugging) |
| `AVA_OC_SANDBOX` | `my-assistant` | NemoClaw sandbox name |
| `AVA_OC_AGENT` | `main` | OpenClaw agent id |
| `AVA_OC_SESSION` | `ava-phone` | Stable session id (keeps vLLM prefix cache warm) |
| `AVA_OC_THINKING` | `off` | Disable visible chain-of-thought for snappy replies |
| `AVA_OC_TIMEOUT` | `240` | Agent exec timeout (s) |
| `AVA_GPU_SERVICE` | `http://127.0.0.1:8189` | the GPU service endpoint |
| `AVA_GPU_MODEL` | `lustifygpumodelNSFW_apexV8.safetensors` | the GPU model checkpoint |
| `AVA_PHONE_THRESHOLD` | `0.40` | Voiceprint accept threshold (phone mics) |
| **Learning & email** | | |
| `ANTHROPIC_API_KEY` | _(required for code mode)_ | Claude API key for code analysis + generation (used by `coder.py`) |
| `OUTLOOK_EMAIL` | _(optional)_ | Outlook email account for digest delivery (SMTP) |
| `OUTLOOK_PASSWORD` | _(optional)_ | App-specific password for Outlook SMTP (`*` encrypted in vault) |
| `CODE_MAX_TOKENS` | `8192` | Max tokens per Claude code generation call |
| `CODE_MAX_ITERS` | `18` | Max iterations (tool calls) per code editing session |
| `OC_TIMEOUT` | `600` | Claude code reasoning timeout (s, default 10 min for complex tasks) |
