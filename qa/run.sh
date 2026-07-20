#!/usr/bin/env bash
# Whole-app QA suite — one command, three tiers, one summary.
#   qa/run.sh              everything
#   qa/run.sh --backend    tiers 1+2 + contracts + CLI only (pytest)
#   qa/run.sh --e2e        tier 3 (Playwright vs a real bridge) only
set -u
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

run_backend=1; run_e2e=1
case "${1:-}" in
  --backend) run_e2e=0 ;;
  --e2e)     run_backend=0 ;;
esac

backend_rc=0; e2e_rc=0

if [ "$run_backend" = 1 ]; then
  echo "== QA backend tiers (pytest qa) =="
  "$PY" -m pytest qa -q -p no:cacheprovider
  backend_rc=$?
fi

if [ "$run_e2e" = 1 ]; then
  echo "== QA frontend E2E (real bridge + Playwright) =="
  "$PY" qa/e2e/run_e2e.py
  e2e_rc=$?
fi

echo
echo "== QA summary =="
[ "$run_backend" = 1 ] && echo "  backend: $([ $backend_rc -eq 0 ] && echo PASS || echo FAIL)"
[ "$run_e2e" = 1 ] && echo "  e2e:     $([ $e2e_rc -eq 0 ] && echo PASS || echo FAIL)"
exit $((backend_rc + e2e_rc))
