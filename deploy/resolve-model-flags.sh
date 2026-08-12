#!/usr/bin/env bash
# resolve-model-flags.sh — resolve a model id to its vLLM flags, from ONE table.
#
# Reads deploy/model-flags.conf and answers: which tool-call parser, which
# reasoning parser, what context cap, what extra flags. Three callers use it, so
# the answer cannot differ between the bare-metal and container paths:
#
#   deploy/local-serve.sh      sources it   (bare metal / docker run)
#   deploy/install.sh          runs it      (writes deploy/.env)
#   deploy/docker-compose.yml  consumes     ${AVA_VLLM_MODEL_FLAGS} from that .env
#
# ── Use as a library ──────────────────────────────────────────────────────────
#   . deploy/resolve-model-flags.sh
#   ava_resolve_model_flags "<org>/<model>"
#   echo "$AVA_RESOLVED_TOOL_PARSER"
#
# Sets: AVA_RESOLVED_{FAMILY,TOOL_PARSER,REASONING_PARSER,EXTRA_FLAGS,
#                     NATIVE_CTX,MAX_LEN,VLLM_FLAGS}
#
# ── Use as a command ──────────────────────────────────────────────────────────
#   deploy/resolve-model-flags.sh --env  <org>/<model>   # .env lines
#   deploy/resolve-model-flags.sh --show <org>/<model>   # human table
#
# ── Overrides ─────────────────────────────────────────────────────────────────
# Explicit env beats the table. `${VAR+x}` distinguishes "set to empty" (=
# deliberately pass no flag) from "unset" (= fall back to the table):
#
#   AVA_TOOL_PARSER=hermes AVA_REASONING_PARSER= deploy/local-serve.sh
#
# AVA_SERVE_MAX_LEN asks for a context length; the table's native_ctx CLAMPS it
# when known, because vLLM RAISES rather than clamping when asked for more than
# the checkpoint supports — which is how `--profile gpu` came to be unbootable.

_AVA_MFLAGS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
AVA_MODEL_FLAGS_CONF="${AVA_MODEL_FLAGS_CONF:-$_AVA_MFLAGS_DIR/model-flags.conf}"

# Trim leading/trailing whitespace from $1.
_ava_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

ava_resolve_model_flags() {
  local model="${1:-}"
  local lc glob family tool reason ctx extra

  AVA_RESOLVED_FAMILY=""
  AVA_RESOLVED_TOOL_PARSER=""
  AVA_RESOLVED_REASONING_PARSER=""
  AVA_RESOLVED_EXTRA_FLAGS=""
  AVA_RESOLVED_NATIVE_CTX=""
  AVA_RESOLVED_MAX_LEN=""
  AVA_RESOLVED_VLLM_FLAGS=""

  lc="$(printf '%s' "$model" | tr '[:upper:]' '[:lower:]')"

  local tbl_tool="" tbl_reason="" tbl_extra="" tbl_ctx=""
  if [ -r "$AVA_MODEL_FLAGS_CONF" ]; then
    while IFS='|' read -r glob family tool reason ctx extra; do
      glob="$(_ava_trim "${glob:-}")"
      case "$glob" in ''|'#'*) continue ;; esac
      # Unquoted RHS is a glob pattern on purpose — that is the match.
      # shellcheck disable=SC2053
      if [[ "$lc" == $glob ]]; then
        AVA_RESOLVED_FAMILY="$(_ava_trim "${family:-}")"
        tbl_tool="$(_ava_trim "${tool:-}")"
        tbl_reason="$(_ava_trim "${reason:-}")"
        tbl_ctx="$(_ava_trim "${ctx:-}")"
        tbl_extra="$(_ava_trim "${extra:-}")"
        break
      fi
    done < "$AVA_MODEL_FLAGS_CONF"
  else
    echo "[resolve-model-flags] WARNING: table not found: $AVA_MODEL_FLAGS_CONF" >&2
  fi

  # "-" is the table's way of writing "empty" without an ambiguous blank column.
  [ "$tbl_tool"   = "-" ] && tbl_tool=""
  [ "$tbl_reason" = "-" ] && tbl_reason=""
  [ "$tbl_extra"  = "-" ] && tbl_extra=""
  [ "$tbl_ctx"    = "-" ] && tbl_ctx=""

  # Explicit env wins. `+x` treats "set but empty" as a deliberate "no flag".
  if [ -n "${AVA_TOOL_PARSER+x}" ]; then
    AVA_RESOLVED_TOOL_PARSER="$AVA_TOOL_PARSER"
  else
    AVA_RESOLVED_TOOL_PARSER="$tbl_tool"
  fi
  if [ -n "${AVA_REASONING_PARSER+x}" ]; then
    AVA_RESOLVED_REASONING_PARSER="$AVA_REASONING_PARSER"
  else
    AVA_RESOLVED_REASONING_PARSER="$tbl_reason"
  fi
  AVA_RESOLVED_EXTRA_FLAGS="${AVA_SERVE_EXTRA_FLAGS-$tbl_extra}"
  AVA_RESOLVED_NATIVE_CTX="$tbl_ctx"

  # Context: what the caller asked for, clamped to what the checkpoint supports.
  local want="${AVA_SERVE_MAX_LEN:-${OMNI_MAX_LEN:-65536}}"
  AVA_RESOLVED_MAX_LEN="$want"
  if [ -n "$tbl_ctx" ] && [ "$want" -gt "$tbl_ctx" ] 2>/dev/null; then
    AVA_RESOLVED_MAX_LEN="$tbl_ctx"
    echo "[resolve-model-flags] NOTE: ${model} supports ${tbl_ctx} tokens; capping --max-model-len from ${want}." >&2
  fi
  # The agent's system context (persona + tool schemas) is ~29k tokens. Below
  # that, every tool turn truncates and the failure looks like bad model quality.
  if [ -n "$AVA_RESOLVED_MAX_LEN" ] && [ "$AVA_RESOLVED_MAX_LEN" -lt 32768 ] 2>/dev/null; then
    echo "[resolve-model-flags] WARNING: --max-model-len ${AVA_RESOLVED_MAX_LEN} is below 32768." >&2
    echo "[resolve-model-flags]   Ava's own system context is ~29k tokens, so tool turns will truncate." >&2
  fi

  # Refuse to guess: an unknown family with no explicit override serves WITHOUT
  # tool calling rather than risking a silent mis-parse.
  if [ -z "$AVA_RESOLVED_FAMILY" ] && [ -z "${AVA_TOOL_PARSER+x}" ]; then
    echo "[resolve-model-flags] WARNING: no vLLM parser profile for '${model}'." >&2
    echo "[resolve-model-flags]   Serving WITHOUT tool-calling rather than guessing a parser —" >&2
    echo "[resolve-model-flags]   a wrong --tool-call-parser returns no tool_calls silently and" >&2
    echo "[resolve-model-flags]   Ava's turns would loop until they time out." >&2
    echo "[resolve-model-flags]   Fix: check the model card's vLLM section, then add a row to" >&2
    echo "[resolve-model-flags]   deploy/model-flags.conf, or set for one run:" >&2
    echo "[resolve-model-flags]     AVA_TOOL_PARSER=hermes AVA_REASONING_PARSER= ..." >&2
    echo "[resolve-model-flags]   and set 'tools: none' on this backend in ava.yaml until you do." >&2
  fi

  # The fully-resolved flag string compose interpolates. Built here so compose
  # needs no conditionals — a parser is either present WITH a value or absent
  # from the string entirely. That is what makes "vLLM rejects a bare
  # --reasoning-parser" a non-problem instead of a special case.
  local flags=""
  [ -n "$AVA_RESOLVED_EXTRA_FLAGS" ] && flags="$AVA_RESOLVED_EXTRA_FLAGS"
  if [ -n "$AVA_RESOLVED_TOOL_PARSER" ]; then
    flags="${flags:+$flags }--enable-auto-tool-choice --tool-call-parser $AVA_RESOLVED_TOOL_PARSER"
  fi
  if [ -n "$AVA_RESOLVED_REASONING_PARSER" ]; then
    flags="${flags:+$flags }--reasoning-parser $AVA_RESOLVED_REASONING_PARSER"
  fi
  AVA_RESOLVED_VLLM_FLAGS="$flags"
}

# Run directly (not sourced) -> print for a caller.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  _mode="${1:---show}"
  _model="${2:-}"
  if [ -z "$_model" ]; then
    echo "usage: $0 [--env|--show] <model-id>" >&2
    exit 2
  fi
  ava_resolve_model_flags "$_model"
  case "$_mode" in
    --env)
      printf 'AVA_VLLM_MAX_LEN=%s\n'      "$AVA_RESOLVED_MAX_LEN"
      printf 'AVA_VLLM_MODEL_FLAGS=%s\n'  "$AVA_RESOLVED_VLLM_FLAGS"
      ;;
    --show)
      printf 'model            %s\n' "$_model"
      printf 'family           %s\n' "${AVA_RESOLVED_FAMILY:-<unknown>}"
      printf 'tool-parser      %s\n' "${AVA_RESOLVED_TOOL_PARSER:-<none>}"
      printf 'reasoning-parser %s\n' "${AVA_RESOLVED_REASONING_PARSER:-<none>}"
      printf 'native-ctx       %s\n' "${AVA_RESOLVED_NATIVE_CTX:-<unknown>}"
      printf 'max-model-len    %s\n' "$AVA_RESOLVED_MAX_LEN"
      printf 'extra-flags      %s\n' "${AVA_RESOLVED_EXTRA_FLAGS:-<none>}"
      printf 'vllm-flags       %s\n' "${AVA_RESOLVED_VLLM_FLAGS:-<none>}"
      ;;
    *) echo "usage: $0 [--env|--show] <model-id>" >&2; exit 2 ;;
  esac
fi
