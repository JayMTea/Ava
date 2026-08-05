#!/usr/bin/env bash
# slot.sh — run a SECOND Ava beside the running one (a "staging slot"), so a
# build can be tested while the stable instance keeps serving.
#
#   cd deploy
#   ./slot.sh init                 # write .env.staging from your working .env
#   ./slot.sh up -d --build        # build the working tree into the slot
#   ./slot.sh logs -f ava
#   ./slot.sh down                 # ALWAYS before `docker compose down` on stable
#
# Then open http://127.0.0.1:8097 — note 127.0.0.1, not localhost. Every other
# subcommand is passed straight to `docker compose`.
#
# WHY THIS SCRIPT EXISTS RATHER THAN A DOCUMENTED COMMAND LINE. A slot is four
# separations — project name, AVA_HOME, host port, image tag — and every one of
# them is a corruption or a silent clobber if it is forgotten:
#
#   * no -p                   `up` RECREATES the stable containers
#   * shared AVA_HOME         two bridges over one SQLite store and one session key
#   * shared image tag        a slot build retags the image stable restarts into
#   * shared port             the slot cannot bind, or worse, stable cannot
#
# So the flags are pinned here on EVERY subcommand and checked before each run.
# It is the same argument deploy/profiles/README.md makes for keeping
# COMPOSE_PROFILES in a file: a `down` typed without the flags addresses a
# different stack, and here the other stack is production.
#
# Env: AVA_SLOT names the slot (default "staging"), giving .env.<slot> and the
# compose project ava-<slot>. Two slots at once need different AVA_PORT_HOST
# values in their env files.
set -euo pipefail

cd "$(dirname "$0")" || exit 1

SLOT="${AVA_SLOT:-staging}"
ENV_FILE=".env.${SLOT}"
BASE_ENV=".env"
PROJECT="ava-${SLOT}"

# Defaults from docker-compose.yml, for the "did you actually change it" checks.
STABLE_HOME_DEFAULT="./ava-data"
STABLE_IMAGE_DEFAULT="ava/bridge:latest"

say() { printf '\033[34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

compose() {
  docker compose -f docker-compose.yml -f slot.yml \
                 --env-file "$ENV_FILE" -p "$PROJECT" "$@"
}

# Last assignment of KEY in an env file, comments and surrounding space stripped.
# Returns empty (exit 0) when the file or the key is absent — callers supply the
# compose default themselves, because "unset" and "set to the default" must be
# treated the same by the collision checks below.
_env_get() {
  local key="$1" file="$2"
  [ -f "$file" ] || return 0
  awk -F= -v k="$key" '
    $1 == k { sub(/^[^=]*=/, ""); sub(/[[:space:]]+$/, ""); v = $0 }
    END { print v }
  ' "$file"
}

# ── preflight ────────────────────────────────────────────────────────────────
# Everything here refuses with a named reason rather than letting the slot come
# up wrong. A slot that boots and quietly shares production's state is worse than
# one that does not boot.
preflight() {
  [ -f slot.yml ] || die "slot.yml not found — run this from the deploy/ directory."
  [ -f "$ENV_FILE" ] || die "no $ENV_FILE yet. Create it with:  ./slot.sh init"

  local slot_home slot_image stable_home stable_image stable_profiles
  slot_home="$(_env_get AVA_HOME "$ENV_FILE")"
  slot_image="$(_env_get AVA_IMAGE "$ENV_FILE")"
  stable_home="$(_env_get AVA_HOME "$BASE_ENV")"
  stable_image="$(_env_get AVA_IMAGE "$BASE_ENV")"
  stable_profiles="$(_env_get COMPOSE_PROFILES "$BASE_ENV")"
  stable_home="${stable_home:-$STABLE_HOME_DEFAULT}"
  stable_image="${stable_image:-$STABLE_IMAGE_DEFAULT}"

  [ -n "$slot_home" ] || die "$ENV_FILE sets no AVA_HOME. Both stacks would share $stable_home — two bridges over one database and one session-signing key."
  [ "$slot_home" != "$stable_home" ] || die "$ENV_FILE has AVA_HOME=$slot_home, the same as the stable stack. The slot needs its own state directory."

  [ -n "$slot_image" ] || die "$ENV_FILE sets no AVA_IMAGE. A slot build would retag $stable_image — the image the stable stack restarts into."
  [ "$slot_image" != "$stable_image" ] || die "$ENV_FILE has AVA_IMAGE=$slot_image, the same as the stable stack. A slot build would replace the image stable restarts into."

  # The slot joins the stable network, and compose registers a service's NAME as
  # a network alias — so the slot's `ava` container answers to "ava" there too.
  # Nothing resolves that name under cpu/cuda/rocm/gpu/cloud, so the collision is
  # inert. Under the agent profile it is not: the agent reaches the bridge at
  # http://ava:8096 and the router at http://ava:8010, and embedded DNS would
  # round-robin roughly half of those calls into the slot.
  case ",${stable_profiles}," in
    *,agent,*) die "the stable stack runs the 'agent' profile. Its agent resolves the bridge by the service name 'ava', which the slot would also answer to on the shared network — about half its calls would land in the slot. Stop the agent profile before using a slot." ;;
  esac
}

# ── subcommands ──────────────────────────────────────────────────────────────

# The slot's env file must be SELF-CONTAINED: --env-file replaces deploy/.env
# rather than layering on it, so a file holding only the deltas fails to
# interpolate. Copy stable's settings, minus the keys a slot must own, then
# append those. Copying is what keeps AVA_BACKEND_* in step with whatever engine
# stable actually runs — the slot uses that same engine over the shared network.
cmd_init() {
  [ -f "$BASE_ENV" ] || die "no deploy/.env to copy from. Run ./install.sh first."
  [ ! -f "$ENV_FILE" ] || die "$ENV_FILE already exists. Delete it first if you meant to regenerate it."

  local port="${AVA_PORT_HOST:-8097}"
  {
    printf '# Ava — %s slot. Generated by slot.sh init from deploy/.env.\n' "$SLOT"
    printf '# Self-contained on purpose: --env-file REPLACES .env, it does not layer.\n'
    printf '# See deploy/slot.env.example for what each of these is protecting against.\n\n'
    printf '# --- copied from the stable stack (engine, model, resolved flags) ---\n'
    grep -v -E '^[[:space:]]*(#|$)' "$BASE_ENV" \
      | grep -v -E '^[[:space:]]*(COMPOSE_PROFILES|COMPOSE_PROJECT_NAME|AVA_HOME|AVA_IMAGE|AVA_NAME|AVA_ALLOC_INFER|AVA_PORT_HOST)=' \
      || true
    printf '\n# --- the slot deltas ---\n'
    printf 'COMPOSE_PROFILES=\n'
    printf 'AVA_HOME=./ava-data-%s\n' "$SLOT"
    printf 'AVA_IMAGE=ava/bridge:%s\n' "$SLOT"
    printf 'AVA_PORT_HOST=%s\n' "$port"
    printf 'AVA_ALLOC_INFER=0\n'
    printf '# Comment out AVA_NAME when testing persona or prompt behaviour: it is\n'
    printf '# injected into the system prompt, not just displayed.\n'
    printf 'AVA_NAME=Ava (%s)\n' "$SLOT"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  say "Wrote deploy/$ENV_FILE (project $PROJECT, http://127.0.0.1:$port)"
  say "Set AVA_PASSWORD in it to skip the first-run claim token, then:"
  say "  ./slot.sh up -d --build"
}

# The claim link the bridge prints names config.SERVER_PORT — the IN-CONTAINER
# port, pinned to 8096 by docker-compose.yml — so an unclaimed slot tells you to
# open the STABLE instance, which is already claimed and answers 403. Reuse the
# fallback chain from install.sh, with this slot's real host port.
cmd_claim() {
  preflight
  local port token
  port="$(_env_get AVA_PORT_HOST "$ENV_FILE")"
  port="${port:-8097}"

  # MSYS_NO_PATHCONV=1: Git Bash rewrites /data/... into a Windows path, so the
  # read silently fails and lands on the "could not read it" branch below.
  token="$(MSYS_NO_PATHCONV=1 compose exec -T ava cat /data/data/setup_claim 2>/dev/null \
           | tr -d '[:space:]' || true)"
  if [ -z "$token" ]; then
    token="$(compose logs ava 2>/dev/null \
             | grep -o 'setup?claim=[A-Za-z0-9_-]\{8,\}' | tail -1 | cut -d= -f2 || true)"
  fi

  if [ -n "$token" ]; then
    say "Open this to set the slot's admin password:"
    say "  http://127.0.0.1:${port}/setup?claim=${token}"
    say "  (token alone, if that link wrapped: $token)"
  elif MSYS_NO_PATHCONV=1 compose exec -T ava test -f /data/data/setup_claim 2>/dev/null; then
    warn "The slot has a first-run token but this shell could not read it. Try:"
    warn "  ./slot.sh logs ava | grep 'setup?claim='"
  else
    say "No first-run gate — open http://127.0.0.1:${port} and sign in."
    say "(that is expected when AVA_PASSWORD is set in $ENV_FILE)"
  fi
}

# A local build carries no version: docker-compose.yml passes AVA_VERSION as a
# build arg but defaults it EMPTY, so version.py falls back to the VERSION file
# and a slot claims the same 0.1.0 as the signed release — with nothing in the UI
# telling them apart. Stamp it instead. The plumbing already exists end to end
# (build arg -> Dockerfile ENV -> version.py -> /api/health + Setup → System),
# and version() only lstrips a leading "v", so a +build suffix passes through.
#
# The .dirty marker is the point: it says the slot is running code that is not
# committed, which is the one gap between "what I tested" and "what I will ship"
# that is worth seeing.
_stamp() {
  local ver rev dirty=""
  ver="$(tr -d '[:space:]' < ../VERSION 2>/dev/null || echo '0.0.0')"
  rev="$(git -C .. rev-parse --short HEAD 2>/dev/null || echo 'nogit')"
  [ -z "$(git -C .. status --porcelain 2>/dev/null)" ] || dirty=".dirty"
  printf '%s+stg.%s%s' "$ver" "$rev" "$dirty"
}

cmd_up() {
  preflight
  AVA_VERSION="$(_stamp)"
  AVA_REVISION="$(git -C .. rev-parse HEAD 2>/dev/null || true)"
  export AVA_VERSION AVA_REVISION   # shell env beats --env-file, so these win
  say "Slot version stamp: $AVA_VERSION"
  compose up "$@"
  say "Slot is at http://127.0.0.1:$(_env_get AVA_PORT_HOST "$ENV_FILE")"
  say "Use 127.0.0.1, not localhost: cookies ignore the port, so signing in at"
  say "localhost:<slot port> would overwrite the stable stack's session cookie."

  # Only when detached — a foreground `up` never reaches here, and there is
  # nothing to smoke until it is backgrounded anyway.
  case " $* " in
    *" -d "* | *" --detach "*) echo; cmd_smoke ;;
    *) say "When it is up:  ./slot.sh smoke" ;;
  esac
}

# ── smoke ────────────────────────────────────────────────────────────────────
# Four questions a slot must answer before it is worth testing anything in. Each
# one is a failure that LOOKS like success from the outside, which is why they
# are checked rather than eyeballed:
#
#   1. it answers at all                — a slot that never bound
#   2. it is healthy                    — bound but not serving
#   3. it is running YOUR tree          — the commonest slot mistake by far: `up`
#                                         without `--build`, so the container is
#                                         the last image and the change you are
#                                         about to sign off was never in it
#   4. it can actually answer a turn    — the failure slot.yml's `networks:` note
#                                         describes at length: detached from the
#                                         stable engine, the slot boots healthy,
#                                         serves the UI, passes its healthcheck,
#                                         and refuses every chat turn with what
#                                         reads like a model error
#
# A check that could not run reports SKIP and never PASS — the rule qa/run.sh
# states for its own tiers, for the same reason: a green line nobody earned is
# worse than a missing one.
_ok=0; _bad=0; _skip=0
pass() { printf '\033[32mPASS\033[0m %s\n' "$*"; _ok=$((_ok + 1)); }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*"; _bad=$((_bad + 1)); }
skip() { printf '\033[33mSKIP\033[0m %s\n' "$*"; _skip=$((_skip + 1)); }

# One JSON string field, without a jq dependency — this has to run in Git Bash
# too. Tolerant of the spacing both compact and pretty encoders produce.
_json_str() {
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -1
}
_json_true() { grep -qE "\"$1\"[[:space:]]*:[[:space:]]*true"; }

cmd_smoke() {
  preflight
  local port base health version want body code
  port="$(_env_get AVA_PORT_HOST "$ENV_FILE")"; port="${port:-8097}"
  # 127.0.0.1, never localhost: cookies ignore the port, so a login against
  # localhost:<slot> would overwrite the stable stack's session cookie.
  base="http://127.0.0.1:${port}"
  say "Smoking $base"

  # 1. Reachable. Give a just-started container time to bind rather than
  #    reporting a race as a failure.
  health=""
  for _ in $(seq 1 30); do
    health="$(curl -fsS --max-time 3 "$base/api/health" 2>/dev/null || true)"
    [ -n "$health" ] && break
    sleep 1
  done
  if [ -z "$health" ]; then
    fail "nothing answered at $base/api/health after 30s"
    say "  ./slot.sh logs ava   — the container may have exited"
    _report; return 1
  fi
  pass "the slot answers on port $port"

  # 2. Healthy.
  if printf '%s' "$health" | _json_true ok; then
    pass "/api/health reports ok"
  else
    fail "/api/health did not report ok: $health"
  fi

  # 3. Is this an Ava bridge at all? A MISSING version is not a shy Ava — it is
  #    something else answering. `version.py` ends in `or "0.0.0+dev"`, so the
  #    field is never empty for us.
  #
  #    Not hypothetical, and not a nicety: the default slot port collided with an
  #    unrelated local service that serves `{"ok":true}` on /api/health. Every
  #    check above it passed, and an earlier draft of this one SKIPPED — so the
  #    run went green against a completely different application. A check that
  #    cannot identify what it is talking to has to fail, not shrug.
  version="$(printf '%s' "$health" | _json_str version)"
  want="$(_stamp)"
  if [ -z "$version" ]; then
    fail "$base is not an Ava bridge — /api/health carries no version: $health"
    say "  something else is on port $port. Pick a free one in $ENV_FILE"
    say "  (AVA_PORT_HOST), or stop whatever holds it."
    _report; return 1
  fi

  # 4. Running the tree you are looking at. `_stamp` is recomputed here, so an
  #    edit made AFTER the build is a mismatch — which is the point.
  if [ "$version" = "$want" ]; then
    pass "running your working tree ($version)"
  else
    fail "the slot is running $version, your tree is $want"
    say "  rebuild it:  ./slot.sh up -d --build"
  fi

  # 5. Is it the slot, or did the port quietly land on production? A stable
  #    instance carries no +stg. stamp.
  case "$version" in
    *+stg.*) pass "this is a slot build, not the stable stack" ;;
    *)       fail "$base is NOT a slot — version $version carries no +stg. stamp" ;;
  esac

  # 6. Can Ava actually answer? The one check that exercises the borrowed engine
  #    over the shared network, and the only one that catches a slot which is
  #    healthy and useless. Needs a session, so it needs the slot's password.
  local pw jar
  pw="$(_env_get AVA_PASSWORD "$ENV_FILE")"
  if [ -z "$pw" ]; then
    skip "no AVA_PASSWORD in $ENV_FILE — cannot sign in to ask whether Ava can answer"
    say "  set it there (or run ./slot.sh claim) to include that check"
    _report; return $(( _bad > 0 ))
  fi

  jar="$(mktemp)"; trap 'rm -f "$jar"' RETURN
  curl -fsS --max-time 5 -c "$jar" -d "password=${pw}" "$base/login" >/dev/null 2>&1 || true
  body="$(curl -fsS --max-time 10 -b "$jar" "$base/api/hub/agent/inference" 2>/dev/null || true)"
  if [ -z "$body" ]; then
    fail "could not sign in to the slot — is AVA_PASSWORD in $ENV_FILE correct?"
  elif printf '%s' "$body" | _json_true ok; then
    pass "Ava can answer — the borrowed engine is reachable from the slot"
  else
    code="$(printf '%s' "$body" | _json_str code)"
    fail "Ava cannot answer (${code:-unknown}): $(printf '%s' "$body" | _json_str detail)"
    case "$code" in
      inference_down)
        say "  the slot cannot reach the stable stack's engine. Check it joined the"
        say "  shared network:  ./slot.sh exec ava getent hosts \$(echo \$AVA_BACKEND_URL)" ;;
    esac
  fi

  _report
  return $(( _bad > 0 ))
}

_report() {
  echo
  if [ "$_bad" -gt 0 ]; then
    printf '\033[31m%s failed\033[0m, %s passed, %s skipped\n' "$_bad" "$_ok" "$_skip"
  else
    printf '\033[32mall %s checks passed\033[0m%s\n' "$_ok" \
      "$([ "$_skip" -gt 0 ] && printf ', %s skipped' "$_skip")"
  fi
}

usage() {
  cat <<'EOF'
slot.sh — a second Ava beside the running one.

  ./slot.sh init              write .env.<slot> from the working .env
  ./slot.sh up -d --build     build the working tree into the slot
  ./slot.sh smoke             is it up, is it YOUR code, can it answer?
  ./slot.sh claim             print the first-run link, on the slot's own port
  ./slot.sh down              stop the slot (do this BEFORE stopping stable)
  ./slot.sh <anything else>   passed through to `docker compose`

`up -d` runs smoke for you. Exit is non-zero on any FAIL, so it gates a script.

Env: AVA_SLOT names the slot (default "staging").
EOF
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  claim) shift; cmd_claim "$@" ;;
  smoke) shift; cmd_smoke "$@" ;;
  up) shift; cmd_up "$@" ;;
  -h | --help | help | "") usage ;;
  *) preflight; compose "$@" ;;
esac
