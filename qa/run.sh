#!/usr/bin/env bash
# Whole-app QA suite — one command, three tiers, one summary.
#   qa/run.sh              everything
#   qa/run.sh --backend    tiers 1+2 + contracts + CLI only (pytest)
#   qa/run.sh --e2e        tier 3 (Playwright vs a real bridge) only
set -u
cd "$(dirname "$0")/.." || exit 1

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

# A tier that ran nothing reports SKIPPED, never PASS. run_e2e.py exits 77 when
# its runner or the built bundle is missing; laundering that as 0 is what made
# the E2E tier read "PASS" in every clone while executing zero specs.
_verdict() {   # $1 = exit code
  case "$1" in
    0)  echo "PASS" ;;
    77) echo "SKIPPED (not run)" ;;
    *)  echo "FAIL" ;;
  esac
}

echo
echo "== QA summary =="
[ "$run_backend" = 1 ] && echo "  backend: $(_verdict $backend_rc)"
[ "$run_e2e" = 1 ] && echo "  e2e:     $(_verdict $e2e_rc)"

# A skip must not fail the suite, but must not be summed into it either — 77
# would otherwise masquerade as 77 failures.
[ "$e2e_rc" = 77 ] && e2e_rc=0
[ "$backend_rc" = 77 ] && backend_rc=0
exit $((backend_rc + e2e_rc))
