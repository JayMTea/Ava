#!/usr/bin/env bash
# Source this helper, then call ava_load_instance_env with the checkout root.
# Instance .env files are trusted shell input. Exported values take precedence.
ava_load_instance_env() {
  local _ava_code_root="$1"
  local _ava_instance_root="${AVA_HOME:-$1}"
  local _ava_exported _ava_shell_flags="$-"
  # Bash's `export -p` prints `declare -x`; inside a function that would restore
  # function-local variables and lose the override on return. Use global export.
  _ava_exported="$(export -p | sed 's/^declare -x /export /')"
  set -a
  if [ "${AVA_LOAD_REPO_ENV:-0}" = 1 ] && [ "$_ava_instance_root" != "$_ava_code_root" ] && [ -f "$_ava_code_root/.env" ]; then
    . "$_ava_code_root/.env"
  fi
  if [ -f "$_ava_instance_root/.env" ]; then
    . "$_ava_instance_root/.env"
  fi
  case "$_ava_shell_flags" in *a*) ;; *) set +a ;; esac
  eval "$_ava_exported"
}
