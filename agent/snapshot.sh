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
BACKUP_DIR="$HOME/.nemoclaw/rebuild-backups/$SANDBOX"

LABEL="auto-$(date +%Y%m%d-%H%M%S)"
echo "[snapshot] creating '$LABEL' for sandbox '$SANDBOX'…"
"$NEMOCLAW" "$SANDBOX" snapshot create --name "$LABEL"

# Prune: keep the newest $KEEP snapshot dirs, delete anything older.
if [ -d "$BACKUP_DIR" ]; then
  mapfile -t OLD < <(ls -1dt "$BACKUP_DIR"/*/ 2>/dev/null | tail -n +"$((KEEP + 1))")
  for d in "${OLD[@]:-}"; do
    [ -n "$d" ] || continue
    echo "[snapshot] pruning old snapshot: $d"
    rm -rf "$d"
  done
fi
echo "[snapshot] done (retain newest $KEEP)."
