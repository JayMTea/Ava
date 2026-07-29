#!/usr/bin/env bash
# Idempotent installer for Ava's native OpenClaw capabilities.
# Safe to re-run any time; REQUIRED after `nemoclaw <name> rebuild` (rebuilds wipe
# in-sandbox config + tools). Deploys everything from this folder:
#
#   mcp_server/ -> one modular MCP server (_server.mjs) that recursively auto-loads
#                  every tool module from its category subfolders (persona/, daily/,
#                  health/, …); deployed to /sandbox/.openclaw/mcp_server/
#   policies/   -> least-privilege egress presets (one per source), all applied
#   skills/     -> native skills installed via `nemoclaw skill install`
#   persona.txt.tmpl -> the assistant's identity template, rendered from ava.yaml
#                       (brand/owner/persona) and written to the agent
#                       workspace as IDENTITY.md, which OpenClaw folds into the
#                       system prompt it generates. Edit ava.yaml to re-brand —
#                       no source edits. (Before OpenClaw removed it, this went
#                       to agents.defaults.systemPromptOverride; see §4/5.)
#
# To add a capability: run ./new-tool.sh <name>, implement it, add a policy if it
# needs network, then re-run ./install.sh. Nothing else to wire up.
#
#   usage: ./install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX="${AVA_OC_SANDBOX:-my-assistant}"
NEMOCLAW="${AVA_NEMOCLAW:-$HOME/.local/bin/nemoclaw}"
# Optional gitignored overlay: private servers/skills/policies (e.g. personal
# apps) that layer on top of the core kit without editing this script.
OVERLAY="${AVA_OVERLAY:-$HERE/../overlay/agent}"
DEST="/sandbox/.openclaw/mcp_server"
NOISE='UNDICI|trace-warnings|ExperimentalWarning|qqbot|Config warning|^[│◇├╮╯ ]*$|─'

echo "[ava] sandbox=$SANDBOX"

# --- 0. Bootstrap guard: the runtime + sandbox must exist before we deploy ----
# This script DEPLOYS Ava's tools/policies/skills INTO an existing NemoClaw
# sandbox. If the CLI or sandbox isn't there yet, stop with a clear next step
# rather than failing deep in a policy-add. (`ava agent provision` runs this.)
if ! command -v "$NEMOCLAW" >/dev/null 2>&1 && [ ! -x "$NEMOCLAW" ]; then
  echo "[ava] ERROR: nemoclaw CLI not found ($NEMOCLAW)." >&2
  echo "[ava]   Install it:  npm install -g nemoclaw   (github.com/NVIDIA/NemoClaw)" >&2
  echo "[ava]   or run:      ava agent provision --install" >&2
  exit 1
fi
if ! "$NEMOCLAW" list --json 2>/dev/null | grep -q "\"$SANDBOX\""; then
  echo "[ava] ERROR: sandbox '$SANDBOX' not found." >&2
  echo "[ava]   Create it:   nemoclaw onboard   (configures inference + creates the sandbox)" >&2
  echo "[ava]   then re-run: cd agent && ./install.sh" >&2
  exit 1
fi

# --- 1. Egress policies (least privilege, one per source) --------------------
# Applied from the core kit and, if present, the overlay — including each dir's
# generated/ subfolder (from `ava connector policies --write`).
shopt -s nullglob
for poldir in "$HERE/policies" "$HERE/policies/generated" "$OVERLAY/policies" "$OVERLAY/policies/generated"; do
  [ -d "$poldir" ] || continue
  for pol in "$poldir"/*.yaml; do
    echo "[ava] applying policy: $(basename "$pol")…"
    "$NEMOCLAW" "$SANDBOX" policy-add --from-file "$pol" --yes 2>&1 | grep -vE "$NOISE" | tail -2 || true
  done
done

# --- 2. Discover the guard proxy (only present inside the sandbox shell) ------
PROXY="$("$NEMOCLAW" "$SANDBOX" exec --no-tty -- bash -lc 'printf %s "$HTTPS_PROXY"' 2>/dev/null \
  | grep -oE 'https?://[^[:space:]]+' | head -1)"
# Fallback = OpenShell's default sandbox-gateway address; export PROXY to
# override if your sandbox uses a different one.
PROXY="${PROXY:-http://10.200.0.1:3128}"
echo "[ava] proxy=$PROXY"

# --- 2b. Internal-callback tokens (scoped, derived from one root secret) -------
# Source of truth = repo data/.internal_token, which the bridge also reads
# (ava_bridge/config.py). Each MCP server receives a derived token for its own
# capability group. The bridge checks route scopes server-side, so a low-risk
# tool token cannot call config/policy/code-change endpoints.
# Must resolve to the SAME dir the bridge reads (ava_bridge/config.py
# _internal_token -> settings.data_dir()). Anchoring on $HERE put it in the code
# root, which is only correct when AVA_HOME is unset — on Docker the bridge reads
# /data/data while this wrote /app/data, so every /internal/* callback 401'd.
TOKEN_FILE="${AVA_DATA_DIR:-${AVA_HOME:-$HERE/..}/data}/.internal_token"
if [ ! -s "$TOKEN_FILE" ]; then
  mkdir -p "$(dirname "$TOKEN_FILE")"
  ( umask 077; openssl rand -hex 32 > "$TOKEN_FILE" )
  echo "[ava] generated data/.internal_token"
fi
INTERNAL_TOKEN="$(tr -d '\n' < "$TOKEN_FILE")"

# --- 2c. Discover MCP servers (core + optional overlay) ----------------------
# A server is just an mcp_server_<category>/ dir with a _server.mjs. Core ships
# some; the overlay can add private ones (personal apps) with no edits here.
# group = category; server name defaults to ava-tools-<category> (a couple of
# legacy names are pinned so tool namespaces stay stable).
declare -A NAME_OVERRIDE=( [admin]=ava-admin )
# Optional overlay server-name pins (keep a personal app's server id stable).
[ -f "$OVERLAY/server-names.sh" ] && source "$OVERLAY/server-names.sh"
declare -A CAT_SRC=()
for base in "$HERE" "$OVERLAY"; do
  [ -d "$base" ] || continue
  for d in "$base"/mcp_server_*/; do
    [ -f "${d}_server.mjs" ] || continue
    cat="$(basename "$d")"; cat="${cat#mcp_server_}"
    CAT_SRC["$cat"]="${d%/}"
  done
done
mapfile -t CATS < <(printf '%s\n' "${!CAT_SRC[@]}" | sort)
[ "${#CATS[@]}" -gt 0 ] || { echo "[ava] ERROR: no mcp_server_* dirs found" >&2; exit 1; }
echo "[ava] discovered ${#CATS[@]} mcp server(s): ${CATS[*]}"

# Token groups = discovered categories + the shared "connectors" group.
GROUPS_JSON="$(printf '%s\n' "${CATS[@]}" connectors | sort -u \
  | python3 -c 'import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
TOKENS_JSON="$(AVA_GROUPS="$GROUPS_JSON" python3 - "$INTERNAL_TOKEN" <<'PY'
import hashlib
import hmac
import json
import os
import sys

base = sys.argv[1]
groups = json.loads(os.environ.get("AVA_GROUPS", "[]"))
tokens = {
    group: hmac.new(base.encode(), f"ava-internal:{group}".encode(), hashlib.sha256).hexdigest()
    for group in groups
}
print(json.dumps(tokens, separators=(",", ":")))
PY
)"

# --- 3. Deploy MCP servers (one independent server per category) -------------
# The set is whatever §2c discovered, so adding/removing a category dir (core or
# overlay) needs no edits here. Each server is small + fully independent.
declare -A SERVER_NAME=()
SPECS=()
for cat in "${CATS[@]}"; do
  name="${NAME_OVERRIDE[$cat]:-ava-tools-$cat}"
  SERVER_NAME["$cat"]="$name"
  src="${CAT_SRC[$cat]}"
  dest="/sandbox/.openclaw/mcp_server_${cat}"
  SPECS+=("{\"name\":\"${name}\",\"path\":\"${dest}/_server.mjs\",\"group\":\"${cat}\"}")
  echo "[ava] deploying mcp_server_${cat} → $dest ($name)…"
  B64="$(tar czf - -C "$src" . | base64 -w0)"
  # Simple deployment: extract to dest, check syntax
  CMD='rm -rf "$DEST"; mkdir -p "$DEST"; echo "$0" | base64 -d | tar xzf - -C "$DEST"; node --check "$DEST/_server.mjs" && echo "[ava] ok: $NAME" || echo "[ava] WARNING: $NAME (syntax error)"'
  "$NEMOCLAW" "$SANDBOX" exec --no-tty -- env DEST="$dest" NAME="$name" bash -c "$CMD" "$B64" 2>&1 | grep -vE "$NOISE" | tail -3
done
SPECS_JSON="[$(IFS=,; echo "${SPECS[*]}")]"

# --- 4 & 5. Register MCP servers + persona + scoped-token tool settings ------
# Add new MCP servers here with the narrowest token group that covers their
# bridge callbacks. Route-level scope checks live in ava_bridge/internal.py.
# Render the persona from persona.txt.tmpl using ava.yaml identity config (brand/
# owner/persona). Prefer the repo venv (has pyyaml) so ava.yaml is honoured; fall
# back to system python3 (renders a neutral persona from built-in defaults).
PY="$HERE/../.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
PROMPT="$("$PY" "$HERE/render_persona.py")"
PROMPT_B64="$(printf %s "$PROMPT" | base64 -w0)"
echo "[ava] registering ${#CATS[@]} mcp servers + persona (${#PROMPT} chars)…"
# tools.toolSearch MUST be false so the model can call MCP tools as native calls
JS="$(cat <<'JS'
const fs = require("fs");

const f = process.env.OPENCLAW_HOME + "/.openclaw/openclaw.json";
const d = JSON.parse(fs.readFileSync(f, "utf8"));
const tokens = JSON.parse(process.env.AVA_TOKENS || "{}");
const servers = JSON.parse(process.env.AVA_SERVERS || "[]");

function server(path, tokenGroup) {
  return {
    command: "node",
    args: [path, "--proxy", process.env.AVA_PROXY, "--internal-token", tokens[tokenGroup] || ""],
  };
}

d.agents = d.agents || {};
delete d.agents.main;
d.agents.defaults = d.agents.defaults || {};

// The persona used to live in agents.defaults.systemPromptOverride. OpenClaw
// removed that key ("OpenClaw owns the generated system prompt"), and leaving
// it behind makes the WHOLE CONFIG INVALID — the agent then refuses to run:
//
//     OpenClaw config is invalid
//       - agents.defaults: Invalid input
//
// `openclaw doctor --fix` repairs that by deleting the key, which silently
// strips Ava's persona and leaves her answering as the base model.
//
// The replacement is a workspace bootstrap file. OpenClaw seeds
// IDENTITY.md / SOUL.md / USER.md into the agent workspace and folds them into
// the system prompt it generates — see the `skipOptionalBootstrapFiles` config
// key, which enumerates exactly those files. Writing the rendered persona to
// IDENTITY.md restores the previous behaviour: verified on a fresh session,
// where the agent went from "I am Nemotron" to "I'm Max, assisting Alpha
// Tester".
delete d.agents.defaults.systemPromptOverride;
const wsDir = "/sandbox/.openclaw/workspace";
fs.mkdirSync(wsDir, { recursive: true });
fs.writeFileSync(wsDir + "/IDENTITY.md", process.env.AVA_P);
d.tools = d.tools || {};
d.tools.toolSearch = false;
d.mcp = d.mcp || {};
d.mcp.servers = d.mcp.servers || {};

// Drop any servers we manage (so a removed overlay app doesn't linger), then
// register the discovered set. Unmanaged servers the user added are preserved.
for (const k of Object.keys(d.mcp.servers)) {
  if (/^ava-/.test(k)) delete d.mcp.servers[k];
}
for (const s of servers) d.mcp.servers[s.name] = server(s.path, s.group);

fs.writeFileSync(f, JSON.stringify(d, null, 2));
console.log("[ava] config written, " + servers.length + " servers registered, persona=" + process.env.AVA_P.length + " chars -> workspace/IDENTITY.md");
JS
)"
JS_B64="$(printf %s "$JS" | base64 -w0)"
"$NEMOCLAW" "$SANDBOX" exec --no-tty -- bash -lc 'AVA_P="$(echo "$0" | base64 -d)" AVA_PROXY="$1" AVA_TOKENS="$3" AVA_SERVERS="$4" node -e "$(echo "$2" | base64 -d)" </dev/null' "$PROMPT_B64" "$PROXY" "$JS_B64" "$TOKENS_JSON" "$SPECS_JSON" 2>&1 | grep -vE "$NOISE" | tail -3

# --- 6. Native skills (core + optional overlay) ------------------------------
# Portable sha256 (Linux sha256sum / macOS shasum) so the deploy manifest below
# matches ava_bridge/skills.py's hash of the same SKILL.md.
_sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
            else shasum -a 256 "$1" | awk '{print $1}'; fi; }
skills_manifest=""
for skroot in "$HERE/skills" "$OVERLAY/skills"; do
  [ -d "$skroot" ] || continue
  for sk in "$skroot"/*/; do
    [ -f "${sk}SKILL.md" ] || continue
    echo "[ava] installing skill: $(basename "$sk")…"
    "$NEMOCLAW" "$SANDBOX" skill install "$sk" 2>&1 | grep -vE "$NOISE" | tail -3 || true
    _name="$(basename "$sk")"; _sum="$(_sha256 "${sk}SKILL.md")"
    skills_manifest="${skills_manifest:+$skills_manifest,}{\"name\":\"$_name\",\"sha256\":\"$_sum\"}"
  done
done
# Record what was deployed INTO the sandbox so the Agent tab can show which
# skills are live vs newly added in the repo (see ava_bridge/skills.py). Written
# host-side to $AVA_HOME/data (the same dir the bridge reads).
DATA_DIR="${AVA_DATA_DIR:-${AVA_HOME:-$HERE/..}/data}"
if mkdir -p "$DATA_DIR" 2>/dev/null; then
  printf '[%s]\n' "$skills_manifest" > "$DATA_DIR/skills_deployed.json"
  echo "[ava] wrote skills deploy manifest -> $DATA_DIR/skills_deployed.json"
fi

# --- 7. Best-effort gateway refresh (don't block; recover can hang) ----------
# mcp.* hot-reloads and the agent runs embedded, so a full recover isn't required.
# IMPORTANT: recover spawns a detached `ssh -f` tunnel that inherits our stdout;
# if that fd is a pipe the script appears to hang forever. Send recover's output
# to a log file (and close stdin) so the pipe closes and install.sh exits cleanly.
echo "[ava] nudging gateway (best-effort, 20s cap; log: /tmp/ava-recover.log)…"
timeout 20 "$NEMOCLAW" "$SANDBOX" recover </dev/null >/tmp/ava-recover.log 2>&1 \
  && echo "[ava] gateway recovered" \
  || echo "[ava] recover skipped/slow (ok — mcp hot-reloads; see /tmp/ava-recover.log)"

echo "[ava] done. Tools auto-load from $DEST; add more with ./new-tool.sh"
