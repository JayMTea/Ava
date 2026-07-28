"""Cross-process lease ownership — a lock directory, not a daemon.

Several processes on one box can each want a heavy model's memory: the bridge, a
render service, a one-shot script. They must agree on who holds what, and they must
keep agreeing when one of them is killed mid-render.

The naive approach — a refcount and an "is it paused" boolean in each process's
memory — fails in a specific and instructive way. Each interpreter maintains its own
copy, so the count can only leak, and the flag is simply *lost* when a process dies
holding it: nothing ever restores what that process released. A scheme like that
survives only as long as the operation it disagrees about happens to be idempotent.

So nothing here is maintained. Every quantity is **derived** from something the
kernel guarantees:

  * the refcount is *how many lease locks are still held*;
  * a dead holder is one whose lock can be taken (the kernel released it on exit);
  * "who owns this eviction" is *who holds that model's lock*.

Two owners of one eviction becomes unrepresentable, and crash recovery needs no
timeout heuristic — a `LOCK_NB` that succeeds **is** the proof the holder is gone.

**`flock`, deliberately not `lockf`.** POSIX record locks are per-*process*: two
threads in one interpreter would both acquire successfully, and closing any file
descriptor to the file drops that process's locks entirely. With a thread-pool
render worker and per-job threads in the same interpreter, that is false
exclusivity — precisely the bug being fixed. `flock` conflicts across separate file
descriptors even within one process, which is what makes it usable here. It is
already the house idiom for cross-process appends (`perf_log`, `audit`).

**Lock ordering, and the one rule that prevents a stall.** `pool.lock` is a leaf: it
is taken last, held for a read-modify-write measured in milliseconds, and **never
held across a subprocess call, an HTTP request, a sleep, or a thread join.** Model
locks are acquired in sorted id order, so two callers evicting the same pair cannot
deadlock. Every acquisition is non-blocking with a deadline — never a blocking wait,
because waiting behind a peer stuck in a slow stop would hang the very work this
exists to protect. A missed lock means *proceed as before*, never *stall*.

**Deadlines are monotonic, and stamped with the boot id.** `CLOCK_MONOTONIC` shares
an epoch across processes on one boot, so cross-process deadlines are directly
comparable and immune to clock steps and DST. A record from a previous boot is
discarded rather than trusted, so a recycled pid can never be mistaken for a live
holder.

**Scope.** Records and arbitrates ownership. Decides nothing (`policy`), measures
nothing (`capacity`), actuates nothing (`drivers`).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid

from .. import settings

SCHEMA = "alloc/1"

# Never block on a peer: a lock we cannot get in this long means "carry on without
# coordination", which is exactly the pre-allocator behaviour.
LOCK_WAIT_S = float(os.environ.get("AVA_ALLOC_LOCK_WAIT", "5"))
_POLL_S = 0.05


def _dir() -> str:
    """The ledger directory. Every participating process must resolve the same one —
    `ava doctor` cross-checks that, because a divergent path is invisible from
    inside either ledger and silently disables the guarantee."""
    d = settings.get("alloc.ledger_dir", env="AVA_ALLOC_DIR") or \
        os.path.join(settings.home(), "run", "alloc")
    return str(d)


def _path(*parts: str) -> str:
    p = os.path.join(_dir(), *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def now() -> float:
    """Monotonic seconds, comparable across processes on this boot."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def boot_id() -> str:
    """Identifies this boot, so a recycled pid cannot masquerade as a live holder."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        # No /proc (macOS, Windows): fall back to boot time. Coarser, still
        # sufficient to tell "this boot" from "a previous one".
        try:
            import psutil
            return f"boottime-{int(psutil.boot_time())}"
        except Exception:  # noqa: BLE001 — last resort; pid reuse across a reboot
            return "unknown"                       # is then possible but unlikely


class LockBusy(Exception):
    """A peer holds the lock and did not release it within the deadline."""


@contextlib.contextmanager
def flock(name: str, *, wait_s: float = LOCK_WAIT_S, shared: bool = False):
    """Hold an advisory lock on `<ledger>/<name>`, or raise `LockBusy`.

    Non-blocking with a deadline by design — see the module docstring. The file
    descriptor is per-call so the lock is per-holder even within one process.
    """
    path = _path(name)
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = now() + max(0.0, wait_s)
    try:
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except OSError:
                if now() >= deadline:
                    raise LockBusy(name) from None
                time.sleep(_POLL_S)
        try:
            yield fd
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_held(name: str) -> bool:
    """True if some process holds this lock right now.

    The liveness test for a lease: a lock we CAN take is one whose holder is gone,
    because the kernel released it. No heartbeat, no timeout, no clock involved.
    """
    path = _path(name)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


# --- lease records ----------------------------------------------------------- #
def new_lease_id() -> str:
    return uuid.uuid4().hex[:16]


def lease_lock_name(lease_id: str) -> str:
    return os.path.join("leases", f"{lease_id}.lock")


def write_lease(lease_id: str, rec: dict) -> None:
    # `started` is preserved when the record already carries one. It is the age a hold
    # is judged overdue by, and this function is also how a lease is *updated* — so
    # stamping it fresh on every write would mean a lease that is renewed often can
    # never look old, which is exactly backwards.
    rec = {**rec, "schema": SCHEMA, "lease_id": lease_id, "pid": os.getpid(),
           "boot_id": boot_id(), "started": rec.get("started") or now(),
           "wall": rec.get("wall") or time.time()}
    _write_json(os.path.join("leases", f"{lease_id}.json"), rec)


def update_lease(lease_id: str, **fields) -> dict | None:
    """Merge fields into an existing lease. Returns None if it is already gone.

    Deliberately not a create. A lease now has more than one writer — the holder's
    heartbeat, and the broker's actuation thread reporting progress — and both can run
    after the holder has released. A blind write would resurrect a record the kernel
    had already reclaimed, leaving a lease nobody holds blocking the planner forever.

    The read-modify-write is serialised on a per-lease mutex rather than the lease's
    own lock, because that one is held by the holder for the lease's whole life and so
    cannot be used for mutual exclusion between writers.
    """
    if read_lease(lease_id) is None:
        return None             # cheap path, and it avoids creating a mutex for a
                                # lease that no longer exists
    mut = os.path.join("leases", f"{lease_id}.mut")
    try:
        with flock(mut, wait_s=2.0):
            rec = read_lease(lease_id)
            if rec is None:                             # dropped while we waited
                with contextlib.suppress(OSError):
                    os.unlink(_path(mut))               # leave nothing behind
                return None
            rec.update(fields)
            write_lease(lease_id, rec)
            return rec
    except LockBusy:
        return read_lease(lease_id)      # never block a caller over bookkeeping


def read_lease(lease_id: str) -> dict | None:
    return _read_json(os.path.join("leases", f"{lease_id}.json"))


def drop_lease(lease_id: str) -> None:
    for suffix in (".json", ".lock", ".mut"):
        with contextlib.suppress(OSError):
            os.unlink(_path(os.path.join("leases", f"{lease_id}{suffix}")))


def live_leases() -> list[dict]:
    """Every lease whose owner is still alive, reaping the rest as a side effect.

    "Alive" is decided by the lock, not by the record: a record can be stale, a held
    `flock` cannot. Records from a previous boot are dropped outright.
    """
    out, this_boot = [], boot_id()
    d = os.path.join(_dir(), "leases")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        lid = fn[:-5]
        rec = _read_json(os.path.join("leases", fn))
        if rec is None:
            _quarantine(os.path.join("leases", fn))
            continue
        if rec.get("boot_id") not in (this_boot, None):
            drop_lease(lid)          # from a previous boot: cannot be live
            continue
        if is_held(lease_lock_name(lid)):
            out.append(rec)
        else:
            drop_lease(lid)          # holder is gone; the kernel already told us
    return out


def holders_of(model_id: str) -> list[dict]:
    """Live leases that are using `model_id`."""
    return [r for r in live_leases() if model_id in (r.get("models") or [])]


# --- per-model state (what we owe the box) ----------------------------------- #
# Monotonic fields in a model-state record. See breaker._scrub_foreign_boot for
# why these cannot survive a reboot — the same hazard, the same fix.
_MONOTONIC_STATE_FIELDS = ("at",)


def model_state(model_id: str) -> dict:
    rec = _read_json(os.path.join("models", f"{model_id}.json")) or {}
    if rec and rec.get("boot_id") not in (None, boot_id()):
        # A model we stopped before a reboot is STILL STOPPED — a container with
        # `--restart no` does not come back on its own — so unlike a lease this
        # record must survive. Only its clocks are meaningless: zero `at` (an LRU
        # tie-break) and make any pending restore immediately due, since the
        # cooldown existed to coalesce a burst and the reboot ended the burst.
        rec = {k: v for k, v in rec.items() if k not in _MONOTONIC_STATE_FIELDS}
        if "restore_after" in rec:
            rec["restore_after"] = now()
        rec["boot_id"] = boot_id()
    return rec


def set_model_state(model_id: str, **fields) -> dict:
    """Merge fields into a model's state record.

    This is the record of *what we owe the box*: which models we released and
    therefore must bring back. It is on disk rather than in memory precisely so it
    survives the process that made the promise — a broker that starts and finds a
    released model with no live lease knows to restore it, which in-memory state
    could never tell it.

    Serialised on a per-model `.mut` mutex, because this is a read-modify-write
    with more than one writer (a lease thread releasing, the watchdog restoring).
    A lost update here is not cosmetic: dropping `released_by_us` leaves a model
    Ava stopped looking like one the operator stopped, and every restore path
    then refuses it — permanently.

    It must NOT be the model's own `models/<id>.lock`. `_release_one` already
    holds that for the whole actuation, and `flock` conflicts across separate
    descriptors WITHIN one process (the property `ledger.flock`'s docstring
    documents and `test_alloc_lease` asserts), so reusing it would self-deadlock
    on every release until the deadline expired.
    """
    try:
        with flock(os.path.join("models", f"{model_id}.mut"), wait_s=2.0):
            return _set_model_state_unlocked(model_id, fields)
    except LockBusy:
        # Bookkeeping must never block an actuation. Degrades to the old
        # unlocked behaviour, which is what it was doing every time before.
        return _set_model_state_unlocked(model_id, fields)


def _set_model_state_unlocked(model_id: str, fields: dict) -> dict:
    rec = model_state(model_id)
    rec.update(fields)
    rec["schema"] = SCHEMA
    rec["boot_id"] = boot_id()
    rec["updated"] = time.time()
    _write_json(os.path.join("models", f"{model_id}.json"), rec)
    return rec


def released_by_us(model_id: str) -> bool:
    """Did WE release this model?

    The allocator never brings back something it did not itself release. That keeps
    it from starting a model an operator stopped on purpose, and from fighting
    another supervisor over the same process.
    """
    return bool(model_state(model_id).get("released_by_us"))


def owed() -> list[dict]:
    """Models we released that nothing holds a lease on any more — the restore list."""
    live = {m for r in live_leases() for m in (r.get("models") or [])}
    out = []
    d = os.path.join(_dir(), "models")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        mid = fn[:-5]
        rec = _read_json(os.path.join("models", fn)) or {}
        # `releasing` counts as owed: a process that crashed between "I am about
        # to stop this" and "I stopped it" left only that flag, and believing
        # only the completed one strands the model down permanently.
        if (rec.get("released_by_us") or rec.get("releasing")) and mid not in live:
            out.append({"model_id": mid, **rec})
    return out


def reserved_gib(free_now: float, *, exclude: str = "") -> float:
    """Memory already promised to live leases but not yet consumed by them.

    The planner used to work from the raw pool, so two callers asking for two
    different COLD models inside the same window were both told the room existed.
    On the deferred HTTP path that window is minutes — which is precisely the
    scenario `/lease` is documented for.

    The rule is credit-the-drop, not reserve-forever:

        need - max(0, free_at_grant - free_now)

    Immediately after a grant the pool has not moved, so the full `need` is
    reserved and a second caller sees the truth. Once the model actually loads,
    the pool drop cancels the reservation exactly — so nothing is double-counted.
    Double-counting is not cosmetic here: it would drive effective free negative
    and make the planner over-evict, which is the harm this layer exists to
    prevent. If some OTHER process consumed the memory, the drop is credited to
    the wrong lease and we under-reserve, degrading to exactly today's behaviour.

    Clock-free and probe-free: a pure function of the records plus one number.
    """
    total = 0.0
    for rec in live_leases():
        if not rec.get("granted"):
            continue
        if exclude and rec.get("lease_id") == exclude:
            continue
        need = rec.get("need_gib")
        if need is None:
            continue
        base = rec.get("free_at_grant")
        if base is None:
            total += float(need)            # legacy record: reserve it all
        else:
            consumed = max(0.0, float(base) - float(free_now))
            total += max(0.0, float(need) - consumed)
    return total


def snapshot() -> dict:
    """Read-only view for reports and `ava doctor`. Decides nothing."""
    leases = live_leases()
    return {
        "schema": SCHEMA, "dir": _dir(), "boot_id": boot_id(),
        "leases": leases, "lease_count": len(leases),
        "owed": owed(), "writable": _writable(),
        "fstype": _fstype(),
    }


# --- io helpers -------------------------------------------------------------- #
def _write_json(rel: str, obj: dict) -> None:
    """Atomic write: a torn record read by a peer is worse than a lost one."""
    path = _path(rel)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _read_json(rel: str) -> dict | None:
    try:
        with open(_path(rel), encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def _quarantine(rel: str) -> None:
    """Move an unreadable record aside rather than raising or deleting it."""
    with contextlib.suppress(OSError):
        os.replace(_path(rel), _path(f"{rel}.corrupt.{int(time.time())}"))


def _writable() -> bool:
    try:
        p = _path(".probe")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.unlink(p)
        return True
    except OSError:
        return False


def _fstype() -> str | None:
    """The ledger's filesystem, because network filesystems cannot provide the
    `flock` semantics this module depends on. Reported so `ava doctor` can say so
    rather than letting the guarantee fail silently."""
    try:
        d = os.path.realpath(_dir())
        best, kind = "", None
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount, fstype = parts[1], parts[2]
                if d.startswith(mount) and len(mount) > len(best):
                    best, kind = mount, fstype
        return kind
    except OSError:
        return None


NETWORK_FSTYPES = {"nfs", "nfs4", "cifs", "smbfs", "fuse.sshfs", "9p"}


def flock_is_reliable() -> tuple[bool, str]:
    """Can this ledger location actually provide cross-process exclusion?"""
    if not _writable():
        return False, f"ledger dir not writable: {_dir()}"
    fs = _fstype()
    if fs and fs in NETWORK_FSTYPES:
        return False, (f"ledger is on {fs}, where advisory locks are not reliable — "
                       "set alloc.ledger_dir (AVA_ALLOC_DIR) to local storage")
    return True, f"{fs or 'local'}"
