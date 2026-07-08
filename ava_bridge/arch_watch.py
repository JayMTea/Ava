"""Architecture drift watchdog — keeps the SSOT enforced *between* commits.

The commit path is already gated (pre-commit hook + `arch.py sync --commit`
from the agent's update_architecture tool), but drift that happens without a
commit — a service unit removed, a policy file hand-edited, a diagram edited
without re-rendering — would otherwise sit unnoticed until the next commit.

This scheduler runs `agent/docs/arch.py check --json` on an interval:

- stale/missing *rendered diagrams* are derived artifacts, so they are
  self-healed automatically with `arch.py sync --commit`;
- any remaining structural drift (manifest vs code) raises a persistent
  alert in the Ops dashboard via alerts.push_external — a human (or Ava's
  update_architecture tool) has to reconcile the manifest, so nothing is
  auto-mutated.

Same in-process pattern as perf_store.start_scheduler / learning.start_scheduler:
no systemd, no-op on installs without a deployment manifest (fresh forks).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

from . import alerts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCH = os.path.join(ROOT, "agent", "docs", "arch.py")
MANIFEST = os.path.join(ROOT, "agent", "docs", "architecture.yaml")

INTERVAL = float(os.environ.get("AVA_ARCH_WATCH_INTERVAL", "1800"))  # 30 min
ALERT_ID = "arch_drift"

_started = False


def _arch(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # d2 is typically installed per-user; the service environment may not have it.
    env["PATH"] = os.path.expanduser("~/.local/bin") + os.pathsep + env.get("PATH", "")
    return subprocess.run([sys.executable, ARCH, *args], capture_output=True,
                          text=True, timeout=timeout, cwd=ROOT, env=env)


def _check() -> dict | None:
    cp = _arch("check", "--json")
    try:
        return json.loads(cp.stdout)
    except ValueError:
        return None  # no manifest on this install, or arch.py itself failed


def _diagram_only(errors: list[str]) -> bool:
    return bool(errors) and all(
        ("is stale" in e or "missing — run" in e or "missing rendered" in e)
        for e in errors
    )


def run_cycle() -> dict | None:
    """One check(+heal) pass. Returns the final drift report (None = no manifest)."""
    rep = _check()
    if rep is None:
        return None
    if not rep["ok"] and _diagram_only(rep["errors"]):
        # Diagrams are derived from the SSOT — regenerate and commit, then re-check.
        _arch("sync", "--commit", "--message",
              "ava(arch): watchdog re-render — diagrams drifted from SSOT")
        rep = _check() or rep
    if not rep["ok"]:
        errs = rep["errors"]
        more = f" (+{len(errs) - 1} more)" if len(errs) > 1 else ""
        alerts.push_external(
            ALERT_ID,
            f"Architecture drift: {errs[0]}{more}",
            severity="warn",
            ttl=INTERVAL * 1.5,  # stays active while drift persists, self-clears once fixed
        )
    return rep


def start_scheduler() -> None:
    """Start the periodic watchdog. No-op without a deployment manifest."""
    global _started
    if _started or not os.path.isfile(MANIFEST):
        return
    _started = True

    def loop() -> None:
        import time
        while True:
            try:
                run_cycle()
            except Exception as e:  # noqa: BLE001 — the watchdog must never die
                print(f"[arch-watch] cycle failed: {e}", flush=True)
            time.sleep(INTERVAL)

    threading.Thread(target=loop, name="arch-watch", daemon=True).start()
    print(f"[ava-bridge] architecture drift watchdog: every {INTERVAL:.0f}s", flush=True)
