"""Bridge-observed per-app performance writer.

External apps that follow the SDK write their own ``performance.jsonl``. Most
Hub-connected apps never will — so the bridge records what IT can see: every
connector action it proxies (declared actions, discovered tools, MCP calls),
with latency and status. One bounded JSONL per connector, always under the
bridge's own log root:

    ${AVA_LOGS}/apps/<cid>/performance.jsonl

That location is deliberate: it is OUTSIDE $AVA_HOME/connectors/<cid>, so
deleting a connector in the Hub does not delete its history, and re-adding the
same id resumes the same file. Each write registers the file in perf_mgmt's
source ledger, so a brand-new app is picked up on its first call with no
restart and no manifest `perf:` block.

Record shape (same envelope as perf_log.py; category "action"):
    {ts, iso, host, category: "action", app, action, action_seconds, status, ok}
`action_seconds` — not `seconds` — so proxy latency is never mistaken for GPU
render time by the energy/cost estimators.

Writes are best-effort and never raise into the call path; rotation mirrors
perf_log.py's non-destructive scheme and appends are flock-serialised.
"""
import json
import os
import socket
import time

from . import config, perf_mgmt

try:
    import fcntl  # POSIX advisory locking for cross-process-safe appends
except ImportError:  # pragma: no cover — non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

# Rebindable in tests (like perf_store.ROLLUP_DIR / perf_mgmt.LEDGER_PATH).
APPS_DIR: str = os.path.join(config.LOGS_DIR, "apps")
MAX_BYTES = int(os.environ.get("AVA_PERF_LOG_MAX_BYTES", str(32 * 1024 * 1024)))
KEEP = max(1, int(os.environ.get("AVA_PERF_LOG_KEEP", "5")))
_HOST = socket.gethostname()
_registered: set = set()  # cids already pushed to the ledger this process


def app_log_path(cid: str) -> str:
    """The bridge-owned perf log for one connector id."""
    return os.path.join(APPS_DIR, cid, "performance.jsonl")


def _rotate_if_needed(path: str) -> None:
    """Non-destructive bounded rotation (perf_log.py's scheme): live -> .1 -> …
    -> .KEEP; perf_store rolls each segment up before it ages out."""
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return
    except OSError:
        return
    try:
        oldest = f"{path}.{KEEP}"
        if os.path.exists(oldest):
            os.remove(oldest)
    except OSError:
        pass
    for i in range(KEEP - 1, 0, -1):
        src = f"{path}.{i}"
        try:
            if os.path.exists(src):
                os.replace(src, f"{path}.{i + 1}")
        except OSError:
            pass
    try:
        os.replace(path, f"{path}.1")
    except OSError:
        pass


def record_action(cid: str, action: str, seconds: float, status) -> None:
    """Append one bridge-observed action record for a connector. Best-effort;
    never raises into the proxy path."""
    if not cid or not action:
        return
    try:
        status_i = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_i = None
    try:
        rec = {
            "ts": round(time.time(), 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "host": _HOST,
            "category": "action",
            "app": cid,
            "action": action,
            "action_seconds": round(float(seconds), 3),
            "status": status_i,
            "ok": bool(status_i is not None and status_i < 400),
        }
        path = app_log_path(cid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate_if_needed(path)
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass
            f.write(line)
            f.flush()
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        # First write for a connector registers its log at once
        # (in-memory guard keeps the steady state to a pure append).
        if cid not in _registered:
            perf_mgmt.register_source(cid, path)
            _registered.add(cid)
    except Exception:  # noqa: BLE001 — telemetry must never break a tool call
        pass
