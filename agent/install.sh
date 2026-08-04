#!/usr/bin/env bash
# Idempotent installer for Ava's native OpenClaw capabilities.
# Safe to re-run any time; REQUIRED after `nemoclaw <name> rebuild` (rebuilds wipe
# in-sandbox config + tools). Deploys everything from this folder:
#
#   mcp_server_<srv>/ -> one MCP server per scope (admin, content, productivity,
#                  system). Each has a _server.mjs that recursively auto-loads
#                  every tool module from its category subfolders (daily/,
#                  knowledge/, …); deployed to /sandbox/.openclaw/mcp_server_<srv>/.
#                  The trailing underscore is what the discovery glob below
#                  matches — a bare `mcp_server/` is not a server and is skipped.
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
#   usage: ./install.sh [--only persona|policies|servers|skills|all[,...]] [--dry-run]
#
#   --only     deploy just part of the kit. Changing a persona costs one file
#              write; a full run re-pushes five MCP servers, six skills and
#              seven policies, then nudges the gateway. Default: all.
#   --dry-run  print the plan and exit 0. Touches nothing, needs no sandbox and
#              no CLI — which is what makes the scope matrix testable in CI.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX="${AVA_OC_SANDBOX:-my-assistant}"
NEMOCLAW="${AVA_NEMOCLAW:-$HOME/.local/bin/nemoclaw}"
# Optional gitignored overlay: private servers/skills/policies (e.g. personal
# apps) that layer on top of the core kit without editing this script.
OVERLAY="${AVA_OVERLAY:-$HERE/../overlay/agent}"
# NOTE: there is no single destination. Each server deploys to its own
# /sandbox/.openclaw/mcp_server_<category>/ (see the loop below). A `DEST` here
# named a path nothing is ever deployed to, and the closing message printed it.
NOISE='UNDICI|trace-warnings|ExperimentalWarning|qqbot|Config warning|^[│◇├╮╯ ]*$|─'

# Set once, up here: §2c's mcp_server_*/ glob and §6's skills/*/ glob both rely
# on it, and it used to be set inside §1 — so any future change that skips the
# policy section would silently turn it off for the sections that follow.
shopt -s nullglob

# Run a nemoclaw command, print only its interesting output, and return the
# CLI's OWN exit status — not grep's.
#
# Two bugs pointed in opposite directions before this existed:
#   * `… | grep -vE "$NOISE" | tail -N` with no `|| true` (the server deploy and
#     the register call): `grep -v` exits 1 when it filters EVERYTHING, pipefail
#     propagates that, and `set -e` then aborted an otherwise-successful deploy.
#   * `… || true` (policy-add, skill install): fixed the above by also swallowing
#     genuine failures, which is how a rejected egress policy stayed invisible
#     for months.
# Capturing first and filtering second fixes both: callers that must abort can
# let `set -e` see the real status, and callers that must continue write an
# explicit `|| echo WARNING`.
_run_cli() {
  local out rc                      # NOT `local out=$(...)` — that masks $?
  out="$("$@" 2>&1)"; rc=$?
  printf '%s\n' "$out" | grep -vE "$NOISE" | tail -3 || true
  return $rc
}

# Machine-readable progress, alongside (never instead of) the human `[ava] …`
# lines — a hand-run reads exactly as it always did. ava_bridge/provision_job.py
# splits these on tabs into steps and keeps them out of the log.
#
# A separate channel rather than parsing the prose, because parsing prose is how
# a progress view silently stops updating the day somebody rewords an echo.
#   scope  id  ok|fail  [detail]
_step() { printf '[ava::step]\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "${4:-}"; }

# --- Scope selection ---------------------------------------------------------
# Scope names are shared with `ava_bridge/provision.py` SCOPES and the Hub's
# domain keys. One spelling everywhere.
SCOPES_ALL="persona policies servers skills"
ONLY=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only)    ONLY="${2:-}"; shift 2 ;;
    --only=*)  ONLY="${1#--only=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,31p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "[ava] ERROR: unknown flag '$1' (try --only or --dry-run)" >&2; exit 2 ;;
  esac
done
ONLY="${ONLY:-${AVA_PROVISION_ONLY:-all}}"
# Reject an unknown scope LOUDLY. A future scope name arriving at an older copy
# of this script must fail, never silently fall through to doing everything —
# that is the shell-level half of the capability handshake the remote runtime
# does over /healthz.
for _s in ${ONLY//,/ }; do
  case " all $SCOPES_ALL " in
    *" $_s "*) ;;
    *) echo "[ava] ERROR: unknown scope '$_s' (want: all, ${SCOPES_ALL// /, })" >&2
       exit 2 ;;
  esac
done
_want() { case ",$ONLY," in *,all,*) return 0 ;; *",$1,"*) return 0 ;; *) return 1 ;; esac; }

# Sections are gated on _want; this says which, in one place, for --dry-run and
# for the humans reading a diff. Pure: no CLI, no sandbox, no writes.
_plan() {
  local n=0 d
  for base in "$HERE" "$OVERLAY"; do
    [ -d "$base" ] || continue
    for d in "$base"/mcp_server_*/; do
      [ -f "${d}_server.mjs" ] && n=$((n + 1))
    done
  done
  echo "[ava] plan: scope=$ONLY"
  _want policies && echo "[ava]   §1 apply egress policies"                || true
  echo   "[ava]   §2/2b/2c proxy + tokens + server discovery (always)"
  _want servers  && echo "[ava]   §3 push MCP server bytes"                || true
  # The registration pass ALWAYS registers every discovered server, whatever the
  # scope — it is delete-then-recreate, so registering a subset would unregister
  # the rest. The count below is the guard a test can assert on.
  { _want persona || _want servers; } \
                 && echo "[ava]   §4/5 register servers=$n + write persona"  || true
  _want skills   && echo "[ava]   §6 install skills"                       || true
  { _want servers || [ "$ONLY" = "all" ]; } \
                 && echo "[ava]   §7 nudge gateway"                        || true
  echo "[ava]   servers discovered: $n"
}

if [ "$DRY_RUN" = "1" ]; then
  _plan
  exit 0
fi

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
# The tracked policies carry the DEFAULT bridge port. `server.port` / AVA_PORT is a
# documented knob, and using it used to kill every agent tool silently: the policy
# allowed 8096 while the tool dialled the real port, so the call was blocked, and
# nothing in the failure named a port. Substitute onto a temp copy — never edit the
# tracked file — so a moved bridge is allowed through.
_BRIDGE_PORT_RESOLVED="$(
  cd "$HERE/.." 2>/dev/null && python3 -c \
    'from ava_bridge import config; print(int(config.SERVER_PORT))' 2>/dev/null
)" || _BRIDGE_PORT_RESOLVED=""
: "${_BRIDGE_PORT_RESOLVED:=8096}"
if [ "$_BRIDGE_PORT_RESOLVED" != "8096" ]; then
  echo "[ava] bridge port is ${_BRIDGE_PORT_RESOLVED}; rewriting policy ports from 8096"
fi
_POLTMP="$(mktemp -d)"
trap 'rm -rf "$_POLTMP"' EXIT
POLICY_FAILED=0

if _want policies; then
for poldir in "$HERE/policies" "$HERE/policies/generated" "$OVERLAY/policies" "$OVERLAY/policies/generated"; do
  [ -d "$poldir" ] || continue
  for pol in "$poldir"/*.yaml; do
    echo "[ava] applying policy: $(basename "$pol")…"
    _send="$pol"
    if [ "$_BRIDGE_PORT_RESOLVED" != "8096" ]; then
      _send="$_POLTMP/$(basename "$pol")"
      sed "s/port: 8096\b/port: ${_BRIDGE_PORT_RESOLVED}/g" "$pol" > "$_send"
    fi
    # Deliberately non-fatal: one rejected policy must not strand the other
    # seven or the MCP deploy that follows. But it is REPORTED now — this used
    # to be a bare `|| true`, which is why ava-email-read-only sat unapplied
    # with nothing in Ava saying so.
    if _run_cli "$NEMOCLAW" "$SANDBOX" policy-add --from-file "$_send" --yes; then
      _step policies "$(basename "$pol" .yaml)" ok
    else
      echo "[ava] WARNING: policy $(basename "$pol") was NOT applied (see output above)" >&2
      _step policies "$(basename "$pol" .yaml)" fail "policy-add rejected the file"
      POLICY_FAILED=$((POLICY_FAILED + 1))
    fi
  done
done
if [ "$POLICY_FAILED" -gt 0 ]; then
  echo "[ava] WARNING: ${POLICY_FAILED} egress polic(ies) did not apply — the tools that" >&2
  echo "[ava]          depend on them will be blocked by the sandbox's deny-by-default." >&2
fi
fi  # _want policies

# And tell the tools where the bridge is, so they do not fall back to the default.
# Every generated .mjs reads AVA_BRIDGE_URL first; nothing was setting it.
export AVA_BRIDGE_URL="${AVA_BRIDGE_URL:-http://host.openshell.internal:${_BRIDGE_PORT_RESOLVED}}"

# --- 2. Discover the guard proxy (only present inside the sandbox shell) ------
# The trailing `|| true` is load-bearing: `grep -oE` exits 1 when it matches
# NOTHING, which is exactly what happens when the sandbox is stopped or its
# HTTPS_PROXY is unset. Under `set -o pipefail` that status became the command
# substitution's, and `set -e` aborted the whole install right here — silently,
# because stderr is sent to /dev/null and grep prints nothing on no-match. The
# fallback on the next line, written for precisely this case, was unreachable:
# provisioning against a stopped sandbox died after applying the policies with
# no error message of any kind. Here we genuinely do not care about the exit
# status, only about the captured text, so discarding it is correct.
PROXY="$("$NEMOCLAW" "$SANDBOX" exec --no-tty -- bash -lc 'printf %s "$HTTPS_PROXY"' 2>/dev/null \
  | grep -oE 'https?://[^[:space:]]+' | head -1 || true)"
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

# Server names + registration specs. Computed HERE, in discovery, because this
# pass is pure — it needs no sandbox and must run unconditionally.
#
# ⚠️  DO NOT move this back inside the §3 deploy loop. §4/5 does a
# delete-then-recreate on every /^ava-/ key in d.mcp.servers, driven by
# SPECS_JSON. If SPECS_JSON is ever empty, that registers NOTHING and
# UNREGISTERS EVERY AVA MCP SERVER — including the overlay's private ones. It
# would exit 0, print a green log, and the only symptom would be a chat turn
# hours later where a tool silently doesn't fire.
#
# (`${SPECS[*]}` on an empty array errors under `set -u` on bash 3.2/macOS —
# loud — but yields `[]` on bash 4.4+/Linux — silent. Hence the explicit guard.)
declare -A SERVER_NAME=()
SPECS=()
for cat in "${CATS[@]}"; do
  name="${NAME_OVERRIDE[$cat]:-ava-tools-$cat}"
  SERVER_NAME["$cat"]="$name"
  SPECS+=("{\"name\":\"${name}\",\"path\":\"/sandbox/.openclaw/mcp_server_${cat}/_server.mjs\",\"group\":\"${cat}\"}")
done
SPECS_JSON="[$(IFS=,; echo "${SPECS[*]}")]"
if [ "$SPECS_JSON" = "[]" ]; then
  echo "[ava] ERROR: refusing to register 0 MCP servers — that would unregister" >&2
  echo "[ava]        every ava-* server already in the sandbox." >&2
  exit 1
fi

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
if _want servers; then
for cat in "${CATS[@]}"; do
  name="${SERVER_NAME[$cat]}"
  src="${CAT_SRC[$cat]}"
  dest="/sandbox/.openclaw/mcp_server_${cat}"
  echo "[ava] deploying mcp_server_${cat} → $dest ($name)…"
  B64="$(tar czf - -C "$src" . | base64 -w0)"
  # Simple deployment: extract to dest, check syntax
  CMD='rm -rf "$DEST"; mkdir -p "$DEST"; echo "$0" | base64 -d | tar xzf - -C "$DEST"; node --check "$DEST/_server.mjs" && echo "[ava] ok: $NAME" || echo "[ava] WARNING: $NAME (syntax error)"'
  _run_cli "$NEMOCLAW" "$SANDBOX" exec --no-tty -- env DEST="$dest" NAME="$name" bash -c "$CMD" "$B64"
  _step servers "$name" ok
done
else
  echo "[ava] skipping MCP server byte push (scope=$ONLY)"
fi

# --- 4 & 5. Register MCP servers + persona + scoped-token tool settings ------
# Add new MCP servers here with the narrowest token group that covers their
# bridge callbacks. Route-level scope checks live in ava_bridge/internal.py.
# Render the persona from persona.txt.tmpl using ava.yaml identity config (brand/
# owner/persona). Prefer the repo venv (has pyyaml) so ava.yaml is honoured; fall
# back to system python3 (renders a neutral persona from built-in defaults).
if _want persona || _want servers; then
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
_run_cli "$NEMOCLAW" "$SANDBOX" exec --no-tty -- bash -lc 'AVA_P="$(echo "$0" | base64 -d)" AVA_PROXY="$1" AVA_TOKENS="$3" AVA_SERVERS="$4" node -e "$(echo "$2" | base64 -d)" </dev/null' "$PROMPT_B64" "$PROXY" "$JS_B64" "$TOKENS_JSON" "$SPECS_JSON"
# The persona lands in workspace/IDENTITY.md, which OpenClaw folds into the
# system prompt it generates AT SESSION START — so an existing conversation keeps
# the old voice until it is restarted. Say so rather than implying it is instant.
_step servers "registration" ok "${#CATS[@]} server(s) registered"
if _want persona; then
  echo "[ava] persona written — new sessions pick it up"
  _step persona "IDENTITY.md" ok "${#PROMPT} chars"
fi
else
  echo "[ava] skipping server registration + persona (scope=$ONLY)"
fi

# --- 6. Native skills (core + optional overlay) ------------------------------
# Portable sha256 (Linux sha256sum / macOS shasum) so the deploy manifest below
# matches ava_bridge/skills.py's hash of the same SKILL.md.
_sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
            else shasum -a 256 "$1" | awk '{print $1}'; fi; }
skills_manifest=""
# Written host-side to $AVA_HOME/data (the same dir the bridge reads).
DATA_DIR="${AVA_DATA_DIR:-${AVA_HOME:-$HERE/..}/data}"
if _want skills; then
# Retire skills the repo dropped, BEFORE installing the ones it still has.
# `skill install` is purely additive and nemoclaw has no `skill list`, so a skill
# deleted from source stays in the sandbox forever — still loaded, still telling
# the assistant she has a capability nothing implements any more. The previous
# run's manifest is the only record of what was put in there, which is what makes
# the difference computable at all: repo names now, minus manifest names then.
_repo_skills=""
for skroot in "$HERE/skills" "$OVERLAY/skills"; do
  [ -d "$skroot" ] || continue
  for sk in "$skroot"/*/; do
    [ -f "${sk}SKILL.md" ] || continue
    _repo_skills="${_repo_skills} $(basename "$sk")"
  done
done
# `|| true`: a manifest written by an older version, hand-edited, or truncated by
# a half-finished run must not abort the deploy — worst case nothing is pruned.
_retired="$(AVA_KEEP="$_repo_skills" python3 - "$DATA_DIR/skills_deployed.json" <<'PY' || true
import json, os, sys
keep = set(os.environ.get("AVA_KEEP", "").split())
try:
    with open(sys.argv[1]) as fh:
        prev = json.load(fh)
except Exception:
    prev = []
for row in prev if isinstance(prev, list) else []:
    name = (row or {}).get("name") if isinstance(row, dict) else None
    if name and name not in keep:
        print(name)
PY
)"
for _gone in $_retired; do
  echo "[ava] removing retired skill: $_gone…"
  if _run_cli "$NEMOCLAW" "$SANDBOX" skill remove "$_gone"; then
    _step skills "$_gone" ok removed
  else
    echo "[ava] WARNING: retired skill $_gone was NOT removed (see output above)" >&2
    _step skills "$_gone" fail "skill remove failed"
  fi
done
for skroot in "$HERE/skills" "$OVERLAY/skills"; do
  [ -d "$skroot" ] || continue
  for sk in "$skroot"/*/; do
    [ -f "${sk}SKILL.md" ] || continue
    echo "[ava] installing skill: $(basename "$sk")…"
    # Non-fatal for the same reason as policy-add above, but reported. NOTE the
    # manifest row below is written either way — it records INTENT, which is why
    # ava_bridge/provision.py verifies against the sandbox rather than trusting it.
    if _run_cli "$NEMOCLAW" "$SANDBOX" skill install "$sk"; then
      _step skills "$(basename "$sk")" ok
    else
      echo "[ava] WARNING: skill $(basename "$sk") was NOT installed (see output above)" >&2
      _step skills "$(basename "$sk")" fail "skill install failed"
    fi
    _name="$(basename "$sk")"; _sum="$(_sha256 "${sk}SKILL.md")"
    skills_manifest="${skills_manifest:+$skills_manifest,}{\"name\":\"$_name\",\"sha256\":\"$_sum\"}"
  done
done
# Record what was deployed INTO the sandbox so the Agent tab can show which
# skills are live vs newly added in the repo (see ava_bridge/skills.py), and so
# the next run can tell what to retire.
if mkdir -p "$DATA_DIR" 2>/dev/null; then
  printf '[%s]\n' "$skills_manifest" > "$DATA_DIR/skills_deployed.json"
  echo "[ava] wrote skills deploy manifest -> $DATA_DIR/skills_deployed.json"
fi
else
  echo "[ava] skipping skills (scope=$ONLY)"
fi

# --- 7. Best-effort gateway refresh (don't block; recover can hang) ----------
# mcp.* hot-reloads and the agent runs embedded, so a full recover isn't required.
# IMPORTANT: recover spawns a detached `ssh -f` tunnel that inherits our stdout;
# if that fd is a pipe the script appears to hang forever. Send recover's output
# to a log file (and close stdin) so the pipe closes and install.sh exits cleanly.
# Only worth it when server registration changed; a persona/skill/policy-only run
# has nothing for the gateway to pick up.
if _want servers; then
  echo "[ava] nudging gateway (best-effort, 20s cap; log: /tmp/ava-recover.log)…"
  timeout 20 "$NEMOCLAW" "$SANDBOX" recover </dev/null >/tmp/ava-recover.log 2>&1 \
    && echo "[ava] gateway recovered" \
    || echo "[ava] recover skipped/slow (ok — mcp hot-reloads; see /tmp/ava-recover.log)"
fi

echo "[ava] done. Tools auto-load from ${#CATS[@]} server(s): ${CATS[*]}"
echo "[ava]   add one with:  ./new-tool.sh <name> --category <cat> [--server <srv>]"
