"""Shared configuration: paths, env knobs, constants.

Resolution order for user-facing knobs is env var -> $AVA_HOME/ava.yaml -> default
(via ``settings``), so a forker who edits ava.yaml actually reconfigures the
running bridge. ``settings`` imports nothing from this module, so there is no
import cycle.
"""
import os
import secrets
import shutil

from . import features, settings

# Repo root = parent of this package directory (phone_bridge.py lives there).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# Data root: all runtime state (data/logs/media) lives here. Override with
# AVA_HOME for portable/packaged installs; defaults to the repo root so the
# original single-user layout is unchanged. See docs/INSTALL_REFERENCE.md.
#
# Read from `settings` rather than re-derived here. This module used to compute
# its own `AVA_HOME or ROOT` root and join onto it, which meant it silently
# ignored the `paths.*` keys ava.yaml documents: setting `paths.data` relocated
# chats for skills.py (which asks settings) but not for the bridge (which asked
# this module), and the two then disagreed about where state lived.
# tests/test_path_roots.py fails any module that grows a second root.
DATA_HOME = str(settings.AVA_HOME)

RATE = 16000

# Hard ceiling (seconds) on the ffmpeg decode of an uploaded voice clip, so a
# malformed/huge blob can't wedge the request. Override with AVA_AUDIO_DECODE_TIMEOUT.
AUDIO_DECODE_TIMEOUT = int(os.environ.get("AVA_AUDIO_DECODE_TIMEOUT", "30"))

UPLOAD_DIR = settings.upload_dir()
CHATS_DIR = settings.data_dir()
CHATS_FILE = os.path.join(CHATS_DIR, "chats.json")
LOGS_DIR = settings.logs_dir()
settings.ensure_dirs()

MAX_UPLOAD_BYTES = int(os.environ.get("AVA_MAX_UPLOAD_MB", "25")) * 1024 * 1024
MAX_DOC_CHARS = int(os.environ.get("AVA_MAX_DOC_CHARS", "24000"))

# ---- Branding ----------------------------------------------------------------
# The assistant's display name. Forks re-brand via ava.yaml (brand.name) or the
# AVA_NAME env var; also served to the frontend at /api/brand (see phone_bridge).
AVA_NAME = settings.get("brand.name", "Ava", env="AVA_NAME")
AVA_TAGLINE = settings.get("brand.tagline", "your private, self-hosted assistant",
                           env="AVA_TAGLINE")

# ---- Ava-the-agent (runtime: NemoClaw by default) ---------------------------
# NemoClaw (runs OpenClaw in an OpenShell sandbox) is the default, recommended
# runtime and gives Ava her tools, skills, sandboxed execution, egress policies
# and persistent memory. It's hardware-portable (sandbox from a container image).
#
#   agent.runtime  = which runtime adapter (nemoclaw | direct)
#   agent.required = if true, refuse to fall back to tool-less direct chat when
#                    the runtime is missing (fail loud instead) — the "MUST".
#   agent.enabled  = master switch; false forces the Direct floor (explicit opt-out)
AGENT_RUNTIME = settings.get("agent.runtime", "nemoclaw", env="AVA_AGENT_RUNTIME")
AGENT_REQUIRED = settings.get_bool("agent.required", False, env="AVA_AGENT_REQUIRED")
AGENT_ENABLED = settings.get_bool("agent.enabled", True, env="AVA_AGENT_ENABLED")
OC_SANDBOX = settings.get("agent.sandbox", "my-assistant", env="AVA_OC_SANDBOX")
OC_AGENT = settings.get("agent.agent_id", "main", env="AVA_OC_AGENT")
OC_SESSION = os.environ.get("AVA_OC_SESSION", "ava-phone")
OC_THINKING = os.environ.get("AVA_OC_THINKING", "off")

# Remote agent runtime (Docker "full agent" path): when agent.runtime is
# "remote", the bridge talks over HTTP to a separate agent-runtime container
# (which owns the nemoclaw CLI + Docker socket) instead of running nemoclaw
# in-process. See ava_bridge/runtime/remote.py + deploy/agent.Dockerfile.
# AGENT_TOKEN is defined next to ROUTER_TOKEN (it reuses the internal token as
# the shared bridge<->agent bearer, and that is derived further down).
AGENT_URL = settings.get("agent.url", "http://agent:9100", env="AVA_AGENT_URL")

# Context-window sizing for the chat token counter. CTX_MAX is the model's usable
# context length (override per model; the default matches the shipped model's
# --max-model-len, and deploy/model-flags.conf clamps it to native_ctx); CTX_BASE
# is a rough fixed overhead (system prompt + persona + tool schemas) the UI adds to
# its per-message estimate so the counter tracks OpenClaw's fuller number.
CTX_MAX = settings.get_int("inference.ctx_max", 65536, env="AVA_CTX_MAX")
CTX_BASE = settings.get_int("inference.ctx_base", 2200, env="AVA_CTX_BASE")
# Agentic turns (e.g. character ideation: read recipe -> reason -> preview -> ...)
# can fan out into a dozen+ model round-trips, so allow generous headroom. The UI
# streams Ava's live chain-of-thought during the wait, so a long cap is fine.
OC_TIMEOUT = int(os.environ.get("AVA_OC_TIMEOUT", "600"))
# systemd user services don't inherit ~/.local/bin on PATH, so resolve the CLI.
OC_NEMOCLAW = (
    os.environ.get("AVA_NEMOCLAW")
    or shutil.which("nemoclaw")
    or os.path.expanduser("~/.local/bin/nemoclaw")
)

# ---- Speaker gate ------------------------------------------------------------
# Phone mics differ from the PC enrollment mic, so the gate is a touch more
# lenient here than the live USB-mic loop. Tune from the Hub Voice tab (writes
# voice.threshold in ava.yaml) or override with AVA_PHONE_THRESHOLD.
PHONE_THRESHOLD = settings.get_float("voice.threshold", 0.40,
                                     env="AVA_PHONE_THRESHOLD")

# ---- Inference router --------------------------------------------------------
# The router (ava_bridge/router_app.py) fronts the declared inference backends.
# By default the bridge EMBEDS it in-process at startup (router_host.py); an
# always-on standalone unit is detected and used instead. These URLs derive
# from `inference.router.{host,port}` so one config moves everything.
ROUTER_HOST = settings.get("inference.router.host", "127.0.0.1",
                           env="AVA_ROUTER_HOST")
ROUTER_PORT = settings.get_int("inference.router.port", 8010,
                               env="AVA_ROUTER_PORT")
# The bridge always reaches its (embedded or local standalone) router over
# loopback; ROUTER_HOST is the BIND host, which may be 0.0.0.0 for LAN exposure.
_ROUTER_BASE = f"http://127.0.0.1:{ROUTER_PORT}"
# After a turn we ask the router which backend actually answered so the UI can
# show a "which model" pill. Best-effort: if unreachable, omitted.
ROUTER_WHICH_URL = os.environ.get("AVA_ROUTER_WHICH", f"{_ROUTER_BASE}/which")
# Get/set which backend the router prefers as primary — backs the chat's
# model-picker dropdown.
ROUTER_ROUTE_URL = os.environ.get("AVA_ROUTER_ROUTE", f"{_ROUTER_BASE}/route")
# OpenAI-compatible chat endpoint — used by the DEGRADED chat path (no agent
# runtime): the bridge posts here directly so a fresh fork can chat tool-lessly.
# `inference.chat_url` / AVA_ROUTER_CHAT overrides to bypass the router and hit
# a backend directly — you then lose failover, perf logging and /which.
ROUTER_CHAT_URL = settings.get("inference.chat_url",
                               f"{_ROUTER_BASE}/v1/chat/completions",
                               env="AVA_ROUTER_CHAT")
# Optional bearer key for the direct chat endpoint (e.g. a cloud provider key when
# ROUTER_CHAT_URL points straight at OpenAI/OpenRouter instead of the local router).
INFERENCE_KEY = settings.get("inference.api_key", "", env="AVA_INFERENCE_KEY")

# ---- Server (entrypoint reads these; ava.yaml server.* takes effect) ---------
SERVER_HOST = settings.get("server.host", "127.0.0.1", env="AVA_HOST")
SERVER_PORT = settings.get_int("server.port", 8096, env="AVA_PORT")
PUBLIC_URL = settings.get("server.public_url", f"http://localhost:{SERVER_PORT}",
                          env="AVA_PUBLIC_URL")

# ---- Code mode (Ava edits her own source via Claude) -------------------------
# Code-mode turns run HOST-side (the repo lives here, not in the sandbox) against
# the Anthropic Messages API, scoped to this repo (config.ROOT). Put your key in
# the repo's .env as ANTHROPIC_API_KEY=... (chmod 600); it is auto-loaded.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE = os.environ.get("ANTHROPIC_BASE", "https://api.anthropic.com")
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
# Fallback model list for the UI dropdown if the live /v1/models call fails.
# Some API keys expose short aliases (claude-sonnet-4-6, …) instead of dated
# ids; the UI prefers the live /v1/models list and falls back to these.
CODE_MODELS_FALLBACK = [
    "claude-sonnet-4-6",
    "claude-opus-4-8",
    "claude-haiku-4-5",
]
# Default model for autonomous code-change requests (code_agent). Sonnet is the
# best speed/capability balance for edits; override with AVA_CODE_MODEL in .env.
CODE_MODEL = os.environ.get("AVA_CODE_MODEL", "claude-sonnet-4-6")
CODE_MAX_TOKENS = int(os.environ.get("AVA_CODE_MAX_TOKENS", "8192"))
CODE_MAX_ITERS = int(os.environ.get("AVA_CODE_MAX_ITERS", "18"))
CODE_MAX_FILE_BYTES = int(os.environ.get("AVA_CODE_MAX_FILE_BYTES", str(400 * 1024)))

# Approval mode for edits to Ava's OWN repo (secrets/models/.git are hard-denied
# regardless). "all" = every non-denied edit parks for your approval (safe
# default so a fork never silently self-commits); "policy" = only sensitive
# globs (auth/config/deploy) are gated, other edits auto-commit; "none" =
# auto-apply all non-denied edits (trusted single-owner box).
CODE_APPROVAL = settings.get(
    "code.approval", "all", env="AVA_CODE_APPROVAL").strip().lower()
if CODE_APPROVAL not in ("all", "policy", "none"):
    CODE_APPROVAL = "all"

# --- Self-analysis / learning cycles ---------------------------------------- #
# Ava periodically analyzes her own code activity + chat history (local-first)
# and parks improvement proposals for approval. See ava_bridge/learning.py.
LEARNING_ENABLED = features.enabled("learning")
LEARNING_INTERVAL_H = settings.get_int("learning.interval_hours", 24,
                                       env="AVA_LEARNING_INTERVAL_H")

# --- Personal long-term memory (governed recall) ---------------------------- #
# SQLite FTS5 store at $AVA_HOME/data/memory.db: distilled facts about the
# owner + uploaded-document chunks, folded into chat turns as recall context
# and audit-logged. Inspect/edit/export in the Hub Memory tab. See
# ava_bridge/memory_store.py and docs/MEMORY.md.
MEMORY_ENABLED = features.enabled("memory")
MEMORY_RECALL_K = settings.get_int("memory.recall_k", 4, env="AVA_MEMORY_RECALL_K")
MEMORY_RECALL_MAX_CHARS = settings.get_int("memory.recall_max_chars", 2000,
                                           env="AVA_MEMORY_RECALL_MAX_CHARS")

# ---- Multi-repo code changes -------------------------------------------------
# Ava's code-change engine normally edits her OWN repo (ROOT), auto-applying safe
# edits. It can ALSO edit additional "connected" projects under far stricter rules
# (approval-only, on a throw-away branch, test-gated). Those projects + their
# connector env vars are registered by the optional overlay (see the import guard
# after PROJECTS), so the public core names no specific app.

# ---- Web access (self-hosted SearXNG + host-side reader) ---------------------
# Ava's web layer is HOST-MEDIATED: her sandbox tools only ever call the bridge's
# token-gated /internal/web/* routes (reusing the ava-knowledge egress policy),
# and the HOST does the actual search/fetch. Search hits a PRIVATE, loopback-only
# SearXNG (no third party, no API key); fetch is SSRF-guarded (public IPs only).
# Nothing here is ever exposed to the sandbox — keys/endpoints stay host-side.
WEB_SEARXNG_URL = os.environ.get("AVA_WEB_SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
WEB_MAX_RESULTS = int(os.environ.get("AVA_WEB_MAX_RESULTS", "8"))
WEB_TIMEOUT = int(os.environ.get("AVA_WEB_TIMEOUT", "15"))
# Hard ceiling on bytes read from a fetched page (defence against huge/hostile
# responses). Content is streamed and aborted past this.
WEB_FETCH_MAX_BYTES = int(os.environ.get("AVA_WEB_FETCH_MAX_BYTES", str(2 * 1024 * 1024)))
# Cleaned article text is truncated to this many chars before returning to Ava.
WEB_FETCH_MAX_CHARS = int(os.environ.get("AVA_WEB_FETCH_MAX_CHARS", "20000"))
# Max redirect hops fetch will follow (each hop is re-validated against the SSRF
# guard so a redirect can't bounce into a private address).
WEB_FETCH_MAX_REDIRECTS = int(os.environ.get("AVA_WEB_FETCH_MAX_REDIRECTS", "4"))
# Retry a fetch on transient TRANSPORT errors (Tor circuits are flaky, so a single
# ReadTimeout/ProxyError shouldn't fail an otherwise-fine page). SSRF/policy/HTTP
# status errors are never retried.
WEB_FETCH_RETRIES = int(os.environ.get("AVA_WEB_FETCH_RETRIES", "3"))
# Comma-separated substrings; any fetched/searched hostname containing one is
# refused outright (belt-and-braces on top of the public-IP-only guard).
WEB_DOMAIN_DENYLIST = [
    d.strip().lower() for d in os.environ.get("AVA_WEB_DOMAIN_DENYLIST", "").split(",") if d.strip()
]
# Anonymity: route the host-side page fetch through Tor (SOCKS5h) so pages see a
# Tor exit IP, never your real one. FAIL-CLOSED — if Tor is unreachable the fetch
# errors instead of leaking your IP over clearnet. SearXNG egresses via Tor too
# (searxng/settings.yml outgoing.proxies). Set AVA_WEB_TOR=0 to disable (not
# recommended). socks5h => DNS is resolved inside Tor (no DNS leak to your ISP).
WEB_TOR = os.environ.get("AVA_WEB_TOR", "1").strip().lower() not in ("0", "false", "no", "off")
WEB_TOR_SOCKS = os.environ.get("AVA_WEB_TOR_SOCKS", "socks5h://127.0.0.1:9050")
# Generic browser UA so requests blend in (a unique UA is itself a fingerprint).
WEB_USER_AGENT = os.environ.get(
    "AVA_WEB_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# Human operator name — shown as "Completed by <name>" when a code-change
# proposal is applied after approval (vs "Ava" for her autonomous auto-applies).
OPERATOR_NAME = settings.get("brand.operator", "Admin", env="AVA_OPERATOR_NAME")

# Registry consumed by ava_bridge.code_agent / access_policy. `ava` keeps the
# original self-edit behaviour; connected projects are added by the optional
# overlay ONLY when their checkout exists — so a fresh fork gets just `ava`.
PROJECTS = {
    "ava": {
        "root": ROOT,
        "label": "Ava (self)",
        "approval_only": False,   # safe edits auto-apply + commit
        "branch": False,          # commit straight to the working branch
        "test_cmd": None,
    },
}

# Optional connected-app projects + their connector env
# vars are registered by the gitignored overlay ONLY when their checkout exists;
# a fork simply skips them and gets `ava`-only self-editing.
try:
    from overlay.ava_bridge import personal_config as _personal_config
    _personal_config.apply(PROJECTS, ROOT)
except Exception:  # noqa: BLE001 — no overlay (fork) or a broken overlay
    pass


# ---- Authentication ----------------------------------------------------------
COOKIE_NAME = "ava_session"
SESSION_TTL = int(os.environ.get("AVA_SESSION_TTL_DAYS", "30")) * 86400


def _resolve_cookie_secure() -> bool | None:
    """auth.cookie_secure: true|false|auto. None means auto — decide per request.

    This used to be `os.environ.get("AVA_COOKIE_SECURE", "1") != "0"`, which was
    wrong twice over. It had no ava.yaml key, and it was a bare string compare, so
    `AVA_COOKIE_SECURE=false` evaluated to True — the opposite of what it says.

    Worse, it was unconditional. A Secure cookie is discarded by the browser over
    plain http, so on a LAN IP the session silently vanished and the user bounced
    back to /login with no message anywhere — indistinguishable from a wrong
    password. That is the flow docs/MOBILE.md markets (install the PWA on your
    phone), and qa/env_recipe.py pinned this to "0", so the suite could not see it.

    `auto` matches ava_bridge/router_app.py's require_auth resolution: derive it
    from the request instead of guessing at import. See auth.request_is_secure().
    """
    raw = settings.get("auth.cookie_secure", "auto", env="AVA_COOKIE_SECURE")
    val = str(raw).strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return None


# True = always Secure, False = never, None = auto (per-request scheme).
# The name is unchanged so `mock.patch.object(config, "COOKIE_SECURE", False)`
# keeps working in tests/test_password_change.py.
COOKIE_SECURE = _resolve_cookie_secure()

# Peers whose X-Forwarded-For / X-Forwarded-Proto we believe. Loopback by default,
# which covers `tailscale serve` and a same-host nginx/Caddy. NEVER trust these
# headers from an arbitrary peer: behind a loopback proxy every client would
# otherwise appear to be 127.0.0.1, which would defeat the first-run claim gate
# and let one LAN device exhaust everyone's login-throttle bucket.
TRUSTED_PROXIES = tuple(
    p.strip() for p in str(
        settings.get("server.trusted_proxies", "127.0.0.1,::1",
                     env="AVA_TRUSTED_PROXIES")).split(",") if p.strip())

LOGIN_MAX = 8
LOGIN_WINDOW = 60


# ---- Internal capability endpoints (sandbox MCP tools -> bridge) -------------
# Ava's MCP tools run INSIDE the OpenClaw sandbox and can't see this host's
# upload dir or extraction binaries, so document-reading is exposed as a
# token-gated host service they call back into. agent/install.sh hands the tools
# this same token at launch; only callers presenting it may hit /internal/*.
def _internal_token() -> str:
    env = os.environ.get("AVA_INTERNAL_TOKEN")
    if env:
        return env
    # Resolve through settings.data_dir(), NOT the module-local CHATS_DIR: this
    # file is written host-side by agent/install.sh and read here, so the two must
    # agree under every path override. CHATS_DIR is `AVA_HOME/data` and silently
    # ignores `paths.data` / AVA_DATA_DIR, which agent/install.sh and skills.py
    # both honour — that mismatch made every /internal/* callback 401 on Docker,
    # where AVA_HOME (/data) and the code root (/app) differ.
    path = os.path.join(settings.data_dir(), ".internal_token")
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    os.chmod(path, 0o600)
    return tok


INTERNAL_TOKEN = _internal_token()

# Shared secret the bridge sends to the inference router's control endpoints
# (/which, /route, /fit; also /v1/* when the router is LAN-exposed). MUST match
# the router's own resolution chain (ava_bridge/router_app._resolve_token):
# env -> $AVA_HOME/secrets/router_token -> the internal token.
ROUTER_TOKEN = (os.environ.get("AVA_ROUTER_TOKEN")
                or settings.secret("router_token")
                or INTERNAL_TOKEN)

# Shared bearer between the bridge and the remote agent-runtime shim. Both
# containers mount /data, so the internal token (derived from data/.internal_token)
# is the same on both sides — no separate secret to distribute.
AGENT_TOKEN = os.environ.get("AVA_AGENT_TOKEN") or INTERNAL_TOKEN

# ---- Upload file types -------------------------------------------------------
TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
             ".xml", ".html", ".htm", ".log", ".ini", ".cfg", ".conf", ".toml",
             ".env", ".py", ".js", ".ts", ".mjs", ".cjs", ".sh", ".bash", ".c",
             ".h", ".cpp", ".hpp", ".java", ".go", ".rs", ".rb", ".php", ".sql",
             ".css", ".tsx", ".jsx"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
OFFICE_EXTS = {".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".xls", ".xlsx",
               ".ods", ".odp"}
