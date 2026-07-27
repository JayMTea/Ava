#!/usr/bin/env python3
"""Manual performance-log rollup CLI for Ava (ops / backfill).

The rollup normally runs **in-process** inside the bridge (a daemon thread in
``ava_bridge.perf_store`` — portable, zero-setup, no systemd timer). This script is
just a thin wrapper for running it by hand: an initial backfill, a forced rebuild
after a schema change, or a cron/ops one-off. All the logic lives in ``perf_store``
so there is a single source of truth for the rollup format, retention, and pruning.

Usage:
  python ava_perf_rollup.py            # incremental: finalize newly-cold records
  python ava_perf_rollup.py --rebuild  # wipe rollups + watermark, then rebuild all
"""
from __future__ import annotations

import os
import sys

# Repo root on path so `ava_bridge` imports whether run from here or elsewhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ava_bridge import perf_store


def main() -> int:
    if "--rebuild" in sys.argv:
        for name in (perf_store.HOURLY_FILE, perf_store.DAILY_FILE):
            try:
                os.remove(os.path.join(perf_store.ROLLUP_DIR, name))
            except OSError:
                pass
        try:
            os.remove(perf_store._WATERMARK)
        except OSError:
            pass
        print("[perf-rollup] wiped rollups + watermark; rebuilding from all history")

    res = perf_store.run_rollup()
    print(f"[perf-rollup] {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
