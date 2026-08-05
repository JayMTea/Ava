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
}

usage() {
  cat <<'EOF'
slot.sh — a second Ava beside the running one.

  ./slot.sh init              write .env.<slot> from the working .env
  ./slot.sh up -d --build     build the working tree into the slot
  ./slot.sh claim             print the first-run link, on the slot's own port
  ./slot.sh down              stop the slot (do this BEFORE stopping stable)
  ./slot.sh <anything else>   passed through to `docker compose`

Env: AVA_SLOT names the slot (default "staging").
EOF
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  claim) shift; cmd_claim "$@" ;;
  up) shift; cmd_up "$@" ;;
  -h | --help | help | "") usage ;;
  *) preflight; compose "$@" ;;
esac
