#!/usr/bin/env bash
# Snapshot Ava's agent state (identity + persistent memory + in-sandbox config),
# then prune old snapshots. The sandbox is ephemeral — a `nemoclaw rebuild` wipes
# it — so these snapshots are how Ava's learned state survives a rebuild or disk
# loss. Run on a schedule by ava-snapshot.timer, or manually any time.
#
#   ./agent/snapshot.sh
#
# Restore with:  nemoclaw <sandbox> snapshot restore [version|name|timestamp]
set -euo pipefail

# Load .env if present so AVA_* knobs match the rest of the system.
ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

SANDBOX="${AVA_OC_SANDBOX:-my-assistant}"
NEMOCLAW="${AVA_NEMOCLAW:-$HOME/.local/bin/nemoclaw}"
KEEP="${AVA_SNAPSHOT_KEEP:-14}"

# Refuse rather than build a path out of an empty variable. `BACKUP_DIR` feeds an
# unattended `rm -rf` below, and with `AVA_OC_SANDBOX=""` it collapses to
# `$HOME/.nemoclaw/rebuild-backups/` — whose glob then enumerates EVERY sandbox's
# backups, not this one's. `set -u` does not catch it: the variable is set, it is
# just empty.
case "$SANDBOX" in
  ""|*/*|.|..)
    echo "[snapshot] ERROR: refusing to run with sandbox name '$SANDBOX'." >&2
    echo "[snapshot]   Set AVA_OC_SANDBOX (or agent.sandbox in ava.yaml) to a" >&2
    echo "[snapshot]   plain name — this value builds the path that gets pruned." >&2
    exit 2 ;;
esac
case "$KEEP" in
  ''|*[!0-9]*)
    echo "[snapshot] ERROR: AVA_SNAPSHOT_KEEP must be a number, got '$KEEP'." >&2
    exit 2 ;;
esac
BACKUP_DIR="$HOME/.nemoclaw/rebuild-backups/$SANDBOX"

LABEL="auto-$(date +%Y%m%d-%H%M%S)"
echo "[snapshot] creating '$LABEL' for sandbox '$SANDBOX'…"
"$NEMOCLAW" "$SANDBOX" snapshot create --name "$LABEL"

# Prune: keep the newest $KEEP snapshot dirs, delete anything older.
if [ -d "$BACKUP_DIR" ]; then
  mapfile -t OLD < <(ls -1dt "$BACKUP_DIR"/*/ 2>/dev/null | tail -n +"$((KEEP + 1))")
  for d in "${OLD[@]:-}"; do
    [ -n "$d" ] || continue
    # Never delete outside the directory we just enumerated. `ls -dt` on a glob
    # cannot normally escape it, but this is an unattended `rm -rf` and the cost
    # of being wrong is the owner's snapshot history.
    case "$d" in
      "$BACKUP_DIR"/*) ;;
      *) echo "[snapshot] refusing to prune outside $BACKUP_DIR: $d" >&2; continue ;;
    esac
    echo "[snapshot] pruning old snapshot: $d"
    rm -rf "$d"
  done
fi
echo "[snapshot] done (retain newest $KEEP)."
