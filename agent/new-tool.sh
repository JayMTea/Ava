#!/usr/bin/env bash
# Scaffold a new Ava tool module (and optional egress policy + skill) from templates.
# This is how you "connect a new source" — one module + one narrow policy + one skill.
#
#   usage: ./new-tool.sh <tool_name> [--category <cat>] [--with-policy] [--with-skill]
#   e.g.:  ./new-tool.sh stock_quote --category finance --with-policy --with-skill
#
# After scaffolding:
#   1. Implement mcp_server/<cat>/<tool_name>.mjs (description + inputSchema + handler).
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
while [ $# -gt 0 ]; do
  case "$1" in
    --with-policy) WITH_POLICY=1 ;;
    --with-skill)  WITH_SKILL=1 ;;
    --category)    CATEGORY="${2:-}"; shift ;;
    --category=*)  CATEGORY="${1#*=}" ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$NAME" ]; then
  echo "usage: ./new-tool.sh <tool_name> [--category <cat>] [--with-policy] [--with-skill]" >&2; exit 2
fi
if ! [[ "$NAME" =~ ^[a-z][a-z0-9_]*$ ]]; then
  echo "error: tool name must be lowercase letters/digits/underscores, e.g. stock_quote" >&2; exit 2
fi
if [ -n "$CATEGORY" ] && ! [[ "$CATEGORY" =~ ^[a-z][a-z0-9_]*$ ]]; then
  echo "error: category must be lowercase letters/digits/underscores, e.g. finance" >&2; exit 2
fi

if [ -n "$CATEGORY" ]; then
  TOOL_DIR="$HERE/mcp_server/$CATEGORY"
  REL="mcp_server/$CATEGORY/$NAME.mjs"
else
  TOOL_DIR="$HERE/mcp_server"
  REL="mcp_server/$NAME.mjs"
fi
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
