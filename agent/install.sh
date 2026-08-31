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
#   usage: ./install.sh [--only persona|policies|servers|skills|all[,...]]
#                       [--connector <id>] [--dry-run]
#
#   --only     deploy just part of the kit. Changing a persona costs one file
#              write; a full run re-pushes five MCP servers, six skills and
#              seven policies, then nudges the gateway. Default: all.
#   --connector  narrow --only policies,servers to ONE connected app: its
#              generated egress policy, and the one server its tools live in
#              (mcp_server_connectors). Connecting an app used to cost a full
#              deploy of everything — five servers, six skills, seven policies
#              — to ship two generated files. Requires a scope that includes
#              policies or servers, because there is nothing else it can narrow.
#   --dry-run  print the plan and exit 0. Touches nothing, needs no sandbox and
#              no CLI — which is what makes the scope matrix testable in CI.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX="${AVA_OC_SANDBOX:-my-assistant}"
NEMOCLAW="${AVA_NEMOCLAW:-$HOME/.local/bin/nemoclaw}"
# Optional gitignored overlay: private servers/skills/policies (e.g. personal
# apps) that layer on top of the core kit without editing this script.
OVERLAY="${AVA_OVERLAY:-$HERE/../overlay/agent}"
# GENERATED material — the egress policies and per-app tools rendered from
# connector manifests. It lives under AVA_HOME, not in the checkout, because it
# is runtime state: under Docker the checkout is an image layer, so a rebuild
# threw away every generated tool while keeping the manifests that produced
# them, and on the full-agent profile the bridge and this script run in
# containers with SEPARATE copies of the code root. Mirrors
# ava_bridge/settings.agent_state_dir(); identical to $HERE on a plain checkout,
# where AVA_HOME is the code root and nothing has moved.
STATE="${AVA_AGENT_STATE_DIR:-${AVA_HOME:-$HERE/..}/agent}"

# Realpath, or empty when the directory does not exist. Used to tell "core and
# state are the same directory" (a plain checkout) from "they differ" (Docker),
# so the merges below never copy a tree onto itself.
_realdir() { [ -d "$1" ] && (cd "$1" && pwd -P) || echo ""; }
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
# The category whose server carries every connected app's generated tools.
# Named once: --connector narrows the server push to it, and a fork that renamed
# the directory gets a loud error rather than a silent full deploy.
CONNECTOR_CAT="connectors"
ONLY=""
ONLY_GIVEN=0
CONNECTOR=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only)    ONLY="${2:-}"; ONLY_GIVEN=1; shift 2 ;;
    --only=*)  ONLY="${1#--only=}"; ONLY_GIVEN=1; shift ;;
    --connector)   CONNECTOR="${2:-}"; shift 2 ;;
    --connector=*) CONNECTOR="${1#--connector=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,35p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "[ava] ERROR: unknown flag '$1' (try --only, --connector or --dry-run)" >&2; exit 2 ;;
  esac
done
# `--connector` with no `--only` means "deploy this app", not "deploy everything
# and also narrow two sections of it". Defaulting to `all` there produced a run
# that skipped six of seven policies and four of five servers while printing
# `provisioned` — less than asked, silently, which is the one outcome the scope
# validator below exists to prevent.
if [ -n "$CONNECTOR" ] && [ "$ONLY_GIVEN" = "0" ] && [ -z "${AVA_PROVISION_ONLY:-}" ]; then
  ONLY="policies,servers"
else
  ONLY="${ONLY:-${AVA_PROVISION_ONLY:-all}}"
fi
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

# A connector id is a filename component on both sides of an exec — it selects
# `policies/generated/<id>.yaml` here and names a directory inside the sandbox.
# Same shape ava_bridge/hub/connectors.py `_ID_RE` enforces at creation, checked
# again here because this script is also run by hand: a `..` or a slash that got
# this far would escape the generated directory.
if [ -n "$CONNECTOR" ]; then
  case "$CONNECTOR" in
    *[!a-z0-9_-]*|-*|_*|"")
      echo "[ava] ERROR: '--connector $CONNECTOR' is not a connector id" >&2
      echo "[ava]        (lowercase letters, digits, '-' and '_'; must not start with either)" >&2
      exit 2 ;;
  esac
  # `all` and a narrowing cannot both be honoured: `all` says deploy every
  # policy, every server, every skill; `--connector` says deploy one app's
  # policy and one server. Named rather than resolved for them — picking a side
  # silently is how a run comes to do a fraction of what it reported.
  case ",$ONLY," in
    *,all,*)
      echo "[ava] ERROR: --connector cannot narrow '--only all' — they ask for" >&2
      echo "[ava]        opposite things. Use --only policies,servers (or drop" >&2
      echo "[ava]        --only entirely, which means exactly that)." >&2
      exit 2 ;;
  esac
  # Nothing else narrows, so a scope with neither policies nor servers in it is
  # a request that cannot be honoured. Say so rather than deploying everything
  # or nothing — both are surprises.
  if ! _want policies && ! _want servers; then
    echo "[ava] ERROR: --connector needs a scope that includes policies or servers" >&2
    echo "[ava]        (got --only $ONLY)" >&2
    exit 2
  fi
fi

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
  echo "[ava] plan: scope=$ONLY${CONNECTOR:+ connector=$CONNECTOR}"
  _want policies && echo "[ava]   §1 apply egress policies${CONNECTOR:+ (only $CONNECTOR)}" || true
  echo   "[ava]   §2/2b/2c proxy + tokens + server discovery (always)"
  _want servers  && echo "[ava]   §3 push MCP server bytes${CONNECTOR:+ (only mcp_server_$CONNECTOR_CAT)}" || true
  # The registration pass ALWAYS registers every discovered server, whatever the
  # scope — it is delete-then-recreate, so registering a subset would unregister
  # the rest. The count below is the guard a test can assert on.
  #
  # Two lines, because they are two facts under one `if`: registration runs for
  # `servers` as well as `persona`, and the persona write runs for `persona`
  # alone. One line saying "+ write persona" claimed work a servers-only run
  # both planned and (until this) actually did.
  { _want persona || _want servers; } \
                 && echo "[ava]   §4/5 register servers=$n"                  || true
  _want persona  && echo "[ava]   §4/5 write persona"                        || true
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
  # NOT `npm install -g nemoclaw` — that package is an empty stub, which both
  # docs/AGENT_RUNTIME.md and ava_bridge/runtime/nemoclaw.py say plainly. This
  # line told people to install the stub and then wonder why the CLI was still
  # missing. The real installer is NVIDIA's, and `ava agent provision --install`
  # runs it against the ref deploy/agent.Dockerfile pins.
  echo "[ava]   Install it:  ava agent provision --install" >&2
  echo "[ava]   or by hand:  see docs/AGENT_RUNTIME.md (NVIDIA's installer, not npm)" >&2
  exit 1
fi
# Parse the JSON; do NOT grep it. `grep -q "\"$SANDBOX\""` matches the sandbox
# name ANYWHERE in the output — including inside an error message such as
# `{"error":"sandbox 'ava' not found"}`, and inside a DIFFERENT sandbox's name
# (looking for `ava` matches `ava-old`). ava_bridge/runtime/nemoclaw.py removed
# exactly this check and documents why; the shell copy kept it, so the two halves
# of the same question could disagree. Same three payload shapes the Python side
# handles, and a parse failure means "not found" rather than a false positive.
_sandbox_exists() {
  "$NEMOCLAW" list --json 2>/dev/null | AVA_WANT="$SANDBOX" python3 -c '
import json, os, sys
want = os.environ["AVA_WANT"]
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)
rows = doc if isinstance(doc, list) else (
    doc.get("sandboxes") or doc.get("items") or doc.get("data") or [])
if isinstance(rows, dict):
    rows = list(rows.values())
names = {r.get("name") for r in rows if isinstance(r, dict)}
sys.exit(0 if want in names else 1)
'
}
if ! _sandbox_exists; then
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
POLICY_TOTAL=0
SERVER_FAILED=0

if _want policies; then
# $STATE/policies/generated is where `ava connector policies --write` puts them
# now. On a plain checkout it IS $HERE/policies/generated, so the list is deduped
# by real path — applying the same file twice would double every log line and
# make a rejected policy twice as easy to miss.
_SEEN_POLDIRS=""
for poldir in "$HERE/policies" "$HERE/policies/generated" \
              "$STATE/policies/generated" \
              "$OVERLAY/policies" "$OVERLAY/policies/generated"; do
  _real="$(_realdir "$poldir")"
  [ -n "$_real" ] || continue
  case " $_SEEN_POLDIRS " in *" $_real "*) continue ;; esac
  _SEEN_POLDIRS="$_SEEN_POLDIRS $_real"
  for pol in "$poldir"/*.yaml; do
    # --connector: this app's generated policy and nothing else. Matching on the
    # STEM is right because that is the connector id — `ava connector policies`
    # writes generated/<id>.yaml — while the preset name inside the file is
    # `ava-<id>`, which is not this file's key.
    if [ -n "$CONNECTOR" ] && [ "$(basename "$pol")" != "$CONNECTOR.yaml" ]; then
      continue
    fi
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
    POLICY_TOTAL=$((POLICY_TOTAL + 1))
    if _run_cli "$NEMOCLAW" "$SANDBOX" policy-add --from-file "$_send" --yes; then
      _step policies "$(basename "$pol" .yaml)" ok
    else
      echo "[ava] WARNING: policy $(basename "$pol") was NOT applied (see output above)" >&2
      _step policies "$(basename "$pol" .yaml)" fail "policy-add rejected the file"
      POLICY_FAILED=$((POLICY_FAILED + 1))
    fi
  done
done
# A connector run that matched no policy file is not a no-op to shrug at: the
# app's routes stay denied by default, so its tools will fail one at a time with
# nothing naming the cause. Regenerate is the fix, and it is one command.
# FATAL, not a warning. The policy is half of what a connector deploy delivers:
# without it the sandbox's deny-by-default blocks every route its tools call, so
# the app is installed and unusable. Exiting 0 here let the Hub report a
# successful deploy — and the post-apply verify has no row to catch it either,
# because a policy file that does not exist is not in `desired()`.
if [ -n "$CONNECTOR" ] && [ "$POLICY_TOTAL" -eq 0 ]; then
  echo "[ava] ERROR: no egress policy for '$CONNECTOR' — looked for" >&2
  echo "[ava]        policies/generated/$CONNECTOR.yaml. Without it the sandbox" >&2
  echo "[ava]        denies every route its tools call." >&2
  echo "[ava]        Run: ava connector policies $CONNECTOR --write" >&2
  _step policies "$CONNECTOR" fail "no generated policy file to apply"
  exit 1
fi
if [ "$POLICY_FAILED" -gt 0 ]; then
  echo "[ava] WARNING: ${POLICY_FAILED} egress polic(ies) did not apply — the tools that" >&2
  echo "[ava]          depend on them will be blocked by the sandbox's deny-by-default." >&2
fi
# ONE rejected policy must not strand the other seven or the deploy that follows
# — that stays deliberately non-fatal. But EVERY policy failing is not a partial
# outcome, it is the gateway rejecting the whole egress configuration, and this
# script exited 0 on it: `nemoclaw.provision()` then recorded `step("deploy",
# ok)` and only the post-hoc verify veto noticed anything was wrong.
if [ "$POLICY_TOTAL" -gt 0 ] && [ "$POLICY_FAILED" -eq "$POLICY_TOTAL" ]; then
  echo "[ava] ERROR: every egress policy was rejected (${POLICY_TOTAL}/${POLICY_TOTAL})." >&2
  echo "[ava]        The sandbox has no egress configuration from this checkout," >&2
  echo "[ava]        so deny-by-default will block every tool call." >&2
  exit 1
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
# AVA_INTERNAL_TOKEN WINS, exactly as it does in the bridge's own resolution
# (ava_bridge/config.py `_internal_token()`: env, then the file, then generate).
# The two must agree or the derived tokens below are validated against a secret
# nobody holds.
#
# This exists for the TWO-HOST deployment. config.py notes that "both containers
# mount /data, so the internal token is the same on both sides" — true of a
# single-host compose, false when the bridge and the agent runtime are on
# different machines with their own volumes. There this script would reach the
# `[ ! -s ]` branch, mint its OWN root with `openssl rand`, and hand every MCP
# server a token derived from it. The servers then start perfectly and every
# `/internal/*` callback 401s — a failure quieter than a server that will not
# start at all, because nothing logs it but the tool call that returns nothing.
#
# With this, the root secret is declarable next to AVA_AGENT_TOKEN and
# AVA_ROUTER_TOKEN, which the same deployments already have to pin for the same
# reason. Without it, a copied file is the only thing holding the two hosts
# together and a recreated volume silently breaks every tool.
if [ -n "${AVA_INTERNAL_TOKEN:-}" ]; then
  INTERNAL_TOKEN="$AVA_INTERNAL_TOKEN"
  echo "[ava] internal token: AVA_INTERNAL_TOKEN (env)"
else
  if [ ! -s "$TOKEN_FILE" ]; then
    mkdir -p "$(dirname "$TOKEN_FILE")"
    ( umask 077; openssl rand -hex 32 > "$TOKEN_FILE" )
    echo "[ava] generated data/.internal_token"
  fi
  INTERNAL_TOKEN="$(tr -d '\n' < "$TOKEN_FILE")"
fi

# --- 2b-ii. OpenClaw gateway operator token ----------------------------------
# The bridge reaches OpenClaw's own control plane over a WebSocket when
# `agent.runtime: openclaw_gw`, and that connection authenticates with a token
# the GATEWAY issues — not one we invent. `settings.secret(...)` is deliberately
# called with generate=False for exactly this reason: a self-generated value
# fails every handshake while reporting "the gateway rejected our token", which
# is a lie about which side is wrong.
#
# This script is the right place to fetch it: it already runs where the
# nemoclaw/openclaw CLIs live, and it already writes data/.internal_token with
# the same 0600 discipline. Best-effort — a CLI that cannot mint one leaves the
# file absent, the gateway runtime reports `agent_token_rejected`, and the owner
# pastes one in from Setup -> Agent. A missing token must never fail an install
# whose actual job is deploying tools and policies.
GW_SECRET_DIR="${AVA_SECRETS_DIR:-${AVA_HOME:-$HERE/..}/secrets}"
GW_TOKEN_FILE="$GW_SECRET_DIR/openclaw_gateway_token"
if [ ! -s "$GW_TOKEN_FILE" ]; then
  _gw_tok=""
  if [ -x "$NEMOCLAW" ]; then
    # `gateway-token` is the dedicated command and prints the token alone:
    #   nemoclaw <name> gateway-token   Print the sandbox agent's auth token
    # Verified against nemoclaw v0.0.96's own help output rather than assumed.
    # `|| true` is LOAD-BEARING, and its absence cost every Ava tool on a
    # two-host install. This block is documented three lines up as best-effort,
    # but the script runs under `set -euo pipefail` (line 38): with `pipefail`
    # the pipeline takes nemoclaw's non-zero status, and a command substitution
    # in an assignment propagates it, so `set -e` killed install.sh HERE —
    # before §2c discovered a single MCP server and before §3 deployed one.
    #
    # Observed on the DGX Spark, where `gateway-token` exits 1 with "Could not
    # retrieve the gateway auth token". The five `ava-*` MCP servers therefore
    # kept whatever a NemoClaw migration had left in their argv — the literal
    # string `[STRIPPED_BY_MIGRATION]` — and every one of them failed to start,
    # every 30 minutes, while `install.sh` reported only a generic "issues" and
    # the agent quietly served zero Ava tools.
    #
    # The `dashboard-url` fallback below already had its guard, which is what
    # made the missing one here so easy to miss. Best-effort has to be spelled
    # out in the shell, not just in the comment.
    _gw_tok="$(timeout 20 "$NEMOCLAW" "$SANDBOX" gateway-token --quiet 2>/dev/null \
      | tr -d '\r\n' | head -c 512 || true)"
    # Fallback for a NemoClaw that predates it: `dashboard-url` prints an
    # authenticated URL carrying the same token, which is how NemoClaw hands one
    # to a browser. Kept because the pinned ref is bumped deliberately and an
    # older CLI on the box should degrade rather than silently write nothing.
    if [ -z "$_gw_tok" ]; then
      _gw_url="$(timeout 20 "$NEMOCLAW" "$SANDBOX" dashboard-url --quiet 2>/dev/null || true)"
      _gw_tok="$(printf '%s' "$_gw_url" | sed -n 's/.*[#?&]token=\([A-Za-z0-9._-]*\).*/\1/p' | head -n1)"
    fi
    # Anything that is not a bare token — an error sentence, a usage block — is
    # discarded rather than written to a secret file where it would fail every
    # handshake and read as the gateway's fault.
    case "$_gw_tok" in
      *[!A-Za-z0-9._-]* | "") _gw_tok="" ;;
    esac
  fi
  if [ -n "$_gw_tok" ]; then
    mkdir -p "$GW_SECRET_DIR"
    ( umask 077; printf '%s' "$_gw_tok" > "$GW_TOKEN_FILE" )
    chmod 600 "$GW_TOKEN_FILE" 2>/dev/null || true
    _step gateway token ok "wrote secrets/openclaw_gateway_token"
  else
    _step gateway token skip "no gateway token available; paste one in Setup -> Agent"
  fi
fi

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
# --connector: only the server that carries connector tools. The push is still
# the whole merged mcp_server_connectors tree — stage-validate-swap replaces a
# directory, not a file — so this narrows five server pushes to one, not to one
# app's files. Saying that plainly beats implying a granularity the mechanism
# does not have.
if [ -n "$CONNECTOR" ]; then
  if [ -z "${CAT_SRC[$CONNECTOR_CAT]+x}" ]; then
    echo "[ava] ERROR: --connector needs an mcp_server_$CONNECTOR_CAT server, and" >&2
    echo "[ava]        discovery found none. Run a full deploy instead." >&2
    exit 2
  fi
  echo "[ava] connector run: pushing mcp_server_$CONNECTOR_CAT only"
fi
for cat in "${CATS[@]}"; do
  if [ -n "$CONNECTOR" ] && [ "$cat" != "$CONNECTOR_CAT" ]; then
    continue
  fi
  name="${SERVER_NAME[$cat]}"
  src="${CAT_SRC[$cat]}"
  dest="/sandbox/.openclaw/mcp_server_${cat}"
  echo "[ava] deploying mcp_server_${cat} → $dest ($name)…"
  # The generated per-app tools ($STATE/mcp_server_connectors/apps/) are a
  # MERGE onto the shipped server, not a shadow of it: §2c's discovery requires
  # a _server.mjs, which a generated tree does not have, so a second root there
  # would simply be skipped and the connector tools would never ship. Stage
  # core-then-state into one directory and tar that. State wins on a filename
  # collision, which is the same precedence ava_bridge/provision.py folds on the
  # repo side — the two must agree or every server reads stale.
  payload="$src"
  gen="$STATE/mcp_server_${cat}"
  _gen_real="$(_realdir "$gen")"
  if [ -n "$_gen_real" ] && [ "$_gen_real" != "$(_realdir "$src")" ]; then
    payload="$_POLTMP/stage/mcp_server_${cat}"
    mkdir -p "$payload"
    cp -a "$src"/. "$payload"/
    cp -a "$gen"/. "$payload"/
    echo "[ava]   merged generated material from $gen"
  fi
  # Every module in the payload, computed HOST-side from the staged tree. The
  # sandbox side could glob for them, but that needs `find`/`globstar` in an
  # image whose tooling we do not control — and a glob that silently matched
  # nothing would turn the check below back into the no-op it replaces. We have
  # the tree right here; enumerate it where the answer is certain.
  MJS_LIST="$(cd "$payload" && find . -type f -name '*.mjs' | sed 's|^\./||' | sort | tr '\n' ' ')"
  B64="$(tar czf - -C "$payload" . | base64 -w0)"
  # Stage, VALIDATE, then swap — never destroy the live copy first.
  #
  # This was `rm -rf "$DEST"; mkdir -p "$DEST"; tar xzf`, so the running server
  # was deleted before its replacement existed: a dropped exec, a full disk or an
  # OOM in that window left openclaw.json registering a directory with no code in
  # it. And `node --check` ran AFTER the extraction, so a syntactically broken
  # push replaced a working server and printed a WARNING while the sandbox
  # quietly lost those tools.
  #
  # A rename is the closest thing to atomic available over an exec, and checking
  # before it means a bad payload leaves the previous copy serving. That is the
  # rollback this script never had.
  #
  # EVERY module, not just the entry point. `_server.mjs` discovers tools by
  # recursive import and CATCHES a module that will not load — it writes one
  # line to its own stderr and carries on — so a syntactically broken tool used
  # to deploy cleanly, report success, and then simply not exist as far as Ava
  # was concerned. That silence is the expensive part: nothing between the
  # generator and the missing tool ever said a word. Checking the whole payload
  # makes it a push that fails loudly and keeps the previous copy, which is what
  # the swap was built to allow. ~20ms per file, ~40 files.
  CMD='set -eo pipefail
       rm -rf "$DEST.new" "$DEST.old"
       mkdir -p "$DEST.new"
       echo "$0" | base64 -d | tar xzf - -C "$DEST.new"
       node --check "$DEST.new/_server.mjs"
       for _rel in $AVA_MJS; do
         node --check "$DEST.new/$_rel" || {
           echo "[ava] SYNTAX ERROR in $_rel — not deploying $NAME" >&2
           exit 1
         }
       done
       [ -d "$DEST" ] && mv "$DEST" "$DEST.old" || true
       mv "$DEST.new" "$DEST"
       rm -rf "$DEST.old"
       echo "[ava] ok: $NAME"'
  if _run_cli "$NEMOCLAW" "$SANDBOX" exec --no-tty -- \
       env DEST="$dest" NAME="$name" AVA_MJS="$MJS_LIST" bash -c "$CMD" "$B64"; then
    # Name the app on a connector run. The step stream is keyed on the SERVER,
    # so the Hub reported "Tools · ava-tools-connectors ok" for what the owner
    # asked as "deploy <their app>" — accurate about the mechanism and useless
    # as an answer to the thing they pressed.
    _step servers "$name" ok "${CONNECTOR:+tools for $CONNECTOR}"
  else
    # Non-fatal, and REPORTED. A bare `_run_cli` here aborted the whole script
    # under `set -e`, so one server failing to push skipped registration, the
    # persona and every skill — the half-installed runtime §0 exists to prevent,
    # arriving from the other end. The previous copy is still serving because the
    # swap never happened.
    echo "[ava] WARNING: $name was NOT updated (previous copy left in place)" >&2
    _step servers "$name" fail "push or syntax check failed; previous copy kept"
    SERVER_FAILED=$((SERVER_FAILED + 1))
  fi
done
if [ "$SERVER_FAILED" -gt 0 ]; then
  echo "[ava] WARNING: ${SERVER_FAILED} MCP server(s) kept their previous copy —" >&2
  echo "[ava]          the sandbox is running older tools than this checkout." >&2
fi
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
# The persona is rendered ONLY when it was asked for. This block runs for
# `servers` too, because registration is delete-then-recreate and a subset would
# unregister the rest — but it used to write IDENTITY.md on the way past
# regardless, so deploying a tool server silently applied whatever persona
# ava.yaml happened to hold. The owner would watch the persona's pending count
# clear without ever pressing Apply for it, which is the two-verb rule breaking
# from the inside.
PROMPT=""
PROMPT_B64=""
if _want persona; then
  PY="$HERE/../.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
  PROMPT="$("$PY" "$HERE/render_persona.py")"
  PROMPT_B64="$(printf %s "$PROMPT" | base64 -w0)"
  echo "[ava] registering ${#CATS[@]} mcp servers + persona (${#PROMPT} chars)…"
else
  echo "[ava] registering ${#CATS[@]} mcp servers (persona untouched — not in scope)"
fi
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
// An EMPTY persona means "not in this scope", never "the owner has no persona":
// render_persona.py always produces text, so empty can only come from the guard
// above. Writing it would blank IDENTITY.md and leave Ava answering as the base
// model — the exact failure the deleted systemPromptOverride key used to cause.
const persona = process.env.AVA_P || "";
if (persona) {
  const wsDir = "/sandbox/.openclaw/workspace";
  fs.mkdirSync(wsDir, { recursive: true });
  fs.writeFileSync(wsDir + "/IDENTITY.md", persona);
}
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
console.log("[ava] config written, " + servers.length + " servers registered"
  + (persona ? ", persona=" + persona.length + " chars -> workspace/IDENTITY.md"
             : ", persona untouched"));
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
