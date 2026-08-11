"""One deploy at a time on this machine — across processes, not just threads.

`provision_job` makes the bridge single-slot: one run, observable, 409 on a
concurrent start. That lock is a `threading.Lock`, so it holds for everything
inside the bridge process and for nothing outside it — and two other things run
the same `agent/install.sh` against the same sandbox:

  * `ava agent provision` from a terminal, which is a different process, and
  * the agent-runtime shim, which is a different *container* sharing the socket.

install.sh does `rm -rf "$DEST"` before extracting each MCP server. Two runs
interleaving there means one of them deletes a tree the other is mid-extract
into — a server that ends up half-written or missing while still registered in
openclaw.json, which reads as `stale` forever and is fixed only by another
deploy. The window is small and the trigger is ordinary: press Apply in the Hub,
then run the CLI command the docs print.

An advisory `flock` on a file under AVA_HOME is the right shape because AVA_HOME
is precisely what the racing parties share — the bridge, the CLI and the agent
container all mount it, which is what makes them able to collide in the first
place. It is deliberately NOT a lock file whose existence means "held": a
process killed mid-deploy would leave that behind forever, and the recovery
instruction ("delete this file") is one an owner should never have to receive.
`flock` is released by the kernel when the holder dies, however it dies.

Best-effort by construction: on a platform with no `fcntl` (Windows outside
WSL2), and on a filesystem where locking is unavailable, this yields the lock
rather than refusing to deploy. Serialising is a safety improvement, and a
runtime that cannot deploy at all is worse than the race it prevents.
"""
from __future__ import annotations

import contextlib
import os

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows without WSL2
    fcntl = None  # type: ignore[assignment]


def lock_path() -> str:
    from .. import settings
    return os.path.join(settings.agent_state_dir(), ".deploy.lock")


@contextlib.contextmanager
def held(blocking: bool = False):
    """Yields True when this process owns the deploy, False when someone else
    already does. Never raises: a lock we cannot take is reported, not thrown.
    """
    if fcntl is None:
        yield True
        return
    path = lock_path()
    fh = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, "a+")
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fh.fileno(), flags)
        except OSError:
            yield False
            return
        # Whose it is, for a human reading the file after a hang. Informational
        # only — nothing reads it back to decide anything.
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid {os.getpid()}\n")
            fh.flush()
        except OSError:
            pass
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        # Could not even open the file (read-only mount, missing dir we cannot
        # create). Deploying unserialised is still better than not deploying.
        yield True
    finally:
        if fh is not None:
            with contextlib.suppress(OSError):
                fh.close()
