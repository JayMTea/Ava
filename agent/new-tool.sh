#!/usr/bin/env bash
# Scaffold a new Ava tool module (and optional egress policy + skill) from templates.
# This is how you "connect a new source" — one module + one narrow policy + one skill.
#
#   usage: ./new-tool.sh <tool_name> --category <cat> [--server <srv>]
#                        [--with-policy] [--with-skill]
#   e.g.:  ./new-tool.sh stock_quote --category finance --server productivity \
#            --with-policy --with-skill
#
# Tools live at  mcp_server_<server>/<category>/<tool>.mjs  — the trailing
# underscore is load-bearing, because install.sh discovers servers with
# `mcp_server_*/`. This script used to write to a bare `mcp_server/<cat>/`,
# which that glob never matches: the file appeared, the script said "created",
# and install.sh deployed nothing. Silently. If --server is omitted it is
# inferred from an existing category; when that is ambiguous you are asked.
#
# After scaffolding:
#   1. Implement mcp_server_<srv>/<cat>/<tool_name>.mjs (description + inputSchema + handler).
#   2. If it needs network, edit policies/<tool_name>.yaml (least privilege).
#   3. If you made one, edit skills/<tool_name>/SKILL.md (when/how Ava uses it).
#   4. Run ./install.sh — the server auto-loads the new module, nothing to wire.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAME="${1:-}"
shift || true
WITH_POLICY=0
WITH_SKILL=0
CATEGORY=""
SERVER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --with-policy) WITH_POLICY=1 ;;
    --with-skill)  WITH_SKILL=1 ;;
    --category)    CATEGORY="${2:-}"; shift ;;
    --category=*)  CATEGORY="${1#*=}" ;;
    --server)      SERVER="${2:-}"; shift ;;
    --server=*)    SERVER="${1#*=}" ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

_servers() {   # every deployable server, bare names, one per line
  for d in "$HERE"/mcp_server_*/; do
    [ -f "$d/_server.mjs" ] || continue
    b="$(basename "$d")"; echo "${b#mcp_server_}"
  done
}

_usage() {
  echo "usage: ./new-tool.sh <tool_name> --category <cat> [--server <srv>] [--with-policy] [--with-skill]" >&2
  echo "  servers: $(_servers | paste -sd' ' -)" >&2
}

if [ -z "$NAME" ]; then _usage; exit 2; fi
if ! [[ "$NAME" =~ ^[a-z][a-z0-9_]*$ ]]; then
  echo "error: tool name must be lowercase letters/digits/underscores, e.g. stock_quote" >&2; exit 2
fi
if [ -z "$CATEGORY" ]; then
  echo "error: --category is required — tools live at mcp_server_<server>/<category>/" >&2
  _usage; exit 2
fi
if ! [[ "$CATEGORY" =~ ^[a-z][a-z0-9_]*$ ]]; then
  echo "error: category must be lowercase letters/digits/underscores, e.g. finance" >&2; exit 2
fi

# Resolve the server. Explicit --server wins; otherwise infer from an existing
# category dir, and refuse rather than guess when that is ambiguous or absent.
if [ -z "$SERVER" ]; then
  matches=()
  while IFS= read -r s; do
    [ -d "$HERE/mcp_server_$s/$CATEGORY" ] && matches+=("$s")
  done < <(_servers)
  case "${#matches[@]}" in
    1) SERVER="${matches[0]}"
       echo "[new-tool] category '$CATEGORY' -> server '$SERVER'" ;;
    0) echo "error: no server has a '$CATEGORY' category yet — pass --server to say where it goes." >&2
       echo "  servers: $(_servers | paste -sd' ' -)" >&2
       exit 2 ;;
    *) echo "error: '$CATEGORY' exists under more than one server (${matches[*]}) — pass --server." >&2
       exit 2 ;;
  esac
fi
if ! [[ "$SERVER" =~ ^[a-z][a-z0-9_]*$ ]]; then
  echo "error: server must be lowercase letters/digits/underscores" >&2; exit 2
fi
if [ ! -f "$HERE/mcp_server_$SERVER/_server.mjs" ]; then
  echo "error: no such server 'mcp_server_$SERVER' (needs a _server.mjs)." >&2
  echo "  servers: $(_servers | paste -sd' ' -)" >&2
  echo "  To add one, copy an existing mcp_server_*/ (its _server.mjs names the server)." >&2
  exit 2
fi

TOOL_DIR="$HERE/mcp_server_$SERVER/$CATEGORY"
REL="mcp_server_$SERVER/$CATEGORY/$NAME.mjs"
mkdir -p "$TOOL_DIR"
TOOL="$TOOL_DIR/$NAME.mjs"
if [ -e "$TOOL" ]; then
  echo "error: $TOOL already exists" >&2; exit 1
fi
sed "s/__TOOL_NAME__/$NAME/g" "$HERE/templates/tool.mjs.tmpl" > "$TOOL"
echo "created $REL"

if [ "$WITH_POLICY" = 1 ]; then
  POL="$HERE/policies/$NAME.yaml"
  if [ -e "$POL" ]; then
    echo "note: policies/$NAME.yaml already exists, leaving it"
  else
    sed "s/__TOOL_NAME__/$NAME/g" "$HERE/templates/policy.yaml.tmpl" > "$POL"
    echo "created policies/$NAME.yaml"
  fi
fi

if [ "$WITH_SKILL" = 1 ]; then
  SKILL_DIR="$HERE/skills/$NAME"
  if [ -e "$SKILL_DIR/SKILL.md" ]; then
    echo "note: skills/$NAME/SKILL.md already exists, leaving it"
  else
    mkdir -p "$SKILL_DIR"
    sed "s/__TOOL_NAME__/$NAME/g" "$HERE/templates/skill.md.tmpl" > "$SKILL_DIR/SKILL.md"
    echo "created skills/$NAME/SKILL.md"
  fi
fi

echo
n=1
echo "next:"
echo "  $((n++)). edit $REL (description, inputSchema, handler)"
[ "$WITH_POLICY" = 1 ] && echo "  $((n++)). edit policies/$NAME.yaml (allow only the hosts it needs)"
[ "$WITH_SKILL" = 1 ]  && echo "  $((n++)). edit skills/$NAME/SKILL.md (when/how Ava uses it)"
echo "  $((n++)). ./install.sh"
