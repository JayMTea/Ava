"""Architecture drift watchdog — reports when the SSOT and the code disagree.

The commit path is gated by the pre-commit hook, but drift that happens without
a commit — a service unit removed, a policy file hand-edited, a diagram edited
without re-rendering — would otherwise sit unnoticed until the next commit.

This scheduler runs `agent/docs/arch.py check --json` on an interval and raises
a persistent alert in the Ops dashboard via alerts.push_external. It distinguishes
diagram drift (derived artifacts, reconciled with `python agent/docs/arch.py
sync`) from structural drift (manifest vs code, a real edit), because those are
different jobs for whoever reads the alert.

It REPORTS ONLY. It used to self-heal diagram drift by running
`arch.py sync --commit`, which committed to whatever branch the working tree
happened to be on, authored "Ava (auto-sync)" — right on a settled tree, wrong
mid-refactor, and unstoppable short of restarting the bridge. That path went
with self-editing: nothing in this process commits to the repo any more, and
`arch.paused` went with it because there is no longer anything to pause. The
useful half is the report; a person acts on it.

Same in-process pattern as perf_store.start_scheduler / distill.start_scheduler:
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
    """One check pass. Returns the drift report (None = no manifest on this install)."""
    rep = _check()
    if rep is None:
        return None
    if not rep["ok"]:
        errs = rep["errors"]
        more = f" (+{len(errs) - 1} more)" if len(errs) > 1 else ""
        # Name the fix, because the two kinds of drift need different actions and
        # "Architecture drift" alone tells the reader nothing about which they have.
        fix = ("run `python agent/docs/arch.py sync`" if _diagram_only(errs)
               else "reconcile agent/docs/architecture.yaml with the code")
        alerts.push_external(
            ALERT_ID,
            f"Architecture drift: {errs[0]}{more} — {fix}",
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
