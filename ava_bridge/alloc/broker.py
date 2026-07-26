"""Leases — ask for memory by need, not by naming a victim.

A caller says *"I am about to use this model"* and gets a verdict. It never says
*"stop that other thing"*. That inversion is the point: when the victim is named at
the call site, every new call site has to know the whole topology, and the one that
forgets simply opts out — which is how coordination silently stops covering a newly
added pipeline.

    with alloc.lease("my-model", reason="render") as l:
        if not l:
            return {"error": l.reason}
        run_the_work()

`need_gib` comes from the declared spec, so a model's cost lives in `ava.yaml` once
instead of being implied by conditionals scattered across callers.

**Advisory by default.** Out of the box (`alloc.lease.enforce: false`) this computes
the full decision, records it, and **calls no driver**: nothing is started, stopped
or unloaded. That is deliberate — the sequencing below has to earn trust before it is
allowed to act, and a decision log gathered under real load is how it does that. Turn
on enforcement per install, once its own log looks right.

**Coalescing, and why it is not just an optimisation.** Releasing a heavy model costs
a reload, so a burst of requests must not pay that per request. Holds are
reference-counted, and the restore after the last release is deferred by a cooldown;
a request arriving inside that window reuses the freed state and cancels the pending
restore. The counter is not maintained — it is *derived* from how many lease locks
are still held, so a caller that dies cannot leak a count.

**Never block, never loop, never lie.** Every lock acquisition is bounded; a lock we
cannot get means proceed uncoordinated, exactly as before this layer. A body that
raises still releases. A lease whose holder dies is reclaimed by the kernel. If any
of it fails, the caller's work still runs.

Reads `alloc.lease` from ava.yaml:

    alloc:
      lease:
        enforce: false        # [AVA_ALLOC_ENFORCE] false = decide + record, never act
        cooldown_s: 90        # defer a restore this long, to coalesce a burst
        max_hold_s: 3600      # a hold older than this is reported as overdue

**Scope.** Sequences and records. Decides via `policy`, measures via `capacity`,
actuates via `drivers` — and only when enforcing.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time

from .. import audit, settings
from . import breaker, capacity, ledger, policy, spec
from .base import DriverContext
from .drivers import for_spec

_LOG_NAME = "alloc.jsonl"
_lock = threading.RLock()
_local = threading.local()          # reentrancy: a nested lease is a no-op
_restore_timer: "threading.Timer | None" = None
_actuating: set = set()             # in-flight release threads; see wait_for_actuations

# --- lease state -------------------------------------------------------------- #
# A lease's verdict and its *readiness* are two different facts, and separating them is
# what lets a remote caller be answered immediately. The verdict comes from the planner
# and is known in microseconds; making the room it describes can take minutes, because
# stopping a container and waiting for the kernel to hand the memory back does.
ACTIVE = "active"       # nothing outstanding: the verdict stands and the room exists
PENDING = "pending"     # granted, but a release is still running — do NOT start yet
FAILED = "failed"       # the release raised; the caller is uncoordinated, not blocked


def enforcing() -> bool:
    """Is the allocator allowed to actuate at all? Default False — record only."""
    return settings.get_bool("alloc.lease.enforce", False, env="AVA_ALLOC_ENFORCE")


def evicting() -> bool:
    """May the allocator RELEASE a model to make room? Default False.

    Deliberately a second switch, because the two halves of enforcement carry opposite
    risk. Waiting, or going without, is at worst slower. Releasing something is at
    worst taking memory away from work in progress. So the safe half can be enabled
    first — refusing a start that cannot fit, and restoring what we previously
    released — while releasing stays off until an operator has read their own decision
    log and chosen it.
    """
    return (enforcing()
            and settings.get_bool("alloc.lease.evict", False, env="AVA_ALLOC_EVICT"))


def cooldown_s() -> float:
    return float(settings.get("alloc.lease.cooldown_s", 90) or 0)


def max_hold_s() -> float:
    return float(settings.get("alloc.lease.max_hold_s", 3600) or 0)


class Lease:
    """The verdict for one request. Truthy when granted.

    Truthiness is the whole ergonomic: a caller writes `if not l:` and is done, and a
    call site that ignores the result still runs its work — which is the correct
    default for an advisory layer.
    """

    def __init__(self, model_id: str, granted: bool, reason: str, *,
                 lease_id: str = "", plan: "policy.Plan | None" = None,
                 released: tuple = (), free_gib: float | None = None,
                 enforced: bool = False, nested: bool = False,
                 state: str = ACTIVE):
        self.model_id = model_id
        self.granted = granted
        self.reason = reason
        self.id = lease_id
        self.plan = plan
        self.released = released        # models actually released for this lease
        self.free_gib = free_gib
        self.enforced = enforced        # False = advisory; nothing was actuated
        self.nested = nested
        self.state = state              # ACTIVE / PENDING / FAILED — see the constants

    def __bool__(self) -> bool:
        return self.granted

    @property
    def ready(self) -> bool:
        """Granted *and* the room actually exists. What a caller should start on."""
        return self.granted and self.state != PENDING

    def __repr__(self) -> str:
        return (f"<Lease {self.model_id} "
                f"{'granted' if self.granted else 'denied'}"
                f"{'' if self.state == ACTIVE else ' ' + self.state}"
                f"{'' if self.enforced else ' (advisory)'}: {self.reason}>")


@contextlib.contextmanager
def lease(model_id: str, *, need_gib: float | None = None, reason: str = "",
          priority: str | None = None, log=None):
    """Hold memory for `model_id` for the duration of the block.

    Yields a `Lease`. Always yields — a failure to coordinate is never a failure to
    work, so the caller's body runs regardless and can consult the verdict if it
    cares.
    """
    say = log or (lambda _m: None)

    # Reentrancy: a nested lease on the same model in the same thread is a refcount
    # bump, not a second decision — so an explicit hold around a multi-step pipeline
    # costs one release, and an inner implicit hold cannot deadlock against it.
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    if model_id in held:
        held[model_id] += 1
        try:
            yield Lease(model_id, True, "already held by this thread", nested=True)
        finally:
            held[model_id] -= 1
            if held[model_id] <= 0:
                held.pop(model_id, None)
        return

    lease_obj, lid = _acquire(model_id, need_gib, reason, priority, say)
    held[model_id] = 1
    try:
        yield lease_obj
    finally:
        held.pop(model_id, None)
        with contextlib.suppress(Exception):     # release must never mask the body
            _release(lid, model_id, say)


def admit_plan(model_id: str, *, need_gib: float | None = None) -> policy.Plan:
    """The plan for a hypothetical request. Pure read; actuates nothing.

    Backs `ava alloc plan` and the dashboard's "what would happen" view.
    """
    specs = spec.by_id()
    s = specs.get(model_id)
    if s is None:
        return policy.Plan(model_id, admit=True, gated=False,
                           reason=f"{model_id!r} is not declared — not governed")
    return policy.plan(
        model_id,
        need_gib=need_gib if need_gib is not None else s.need_gib,
        rank=s.rank, free_gib=capacity.free_gib(),
        candidates=_candidates(specs, exclude=model_id),
        speculative_max_s=spec.pool_config().get("speculative_max_s", 15.0))


def restore_due(log=None) -> list[str]:
    """Restore models whose cooldown has elapsed and that nothing is holding.

    The cooldown exists so a burst of requests costs one reload instead of one per
    request, which means *something* has to fire the reload once it lapses. Two things
    do, deliberately:

    * an in-process timer armed at release — prompt, and matches the debounce exactly;
    * this function, called by the allocation watchdog — durable, because a timer dies
      with the process that armed it, and a model left released with nobody holding it
      is the worst state the allocator can leave behind.

    Belt and braces on purpose: the timer alone loses the restore if the process exits,
    and the watchdog alone would delay it by up to its interval.
    """
    say = log or (lambda _m: None)
    done = []
    for rec in ledger.owed():
        mid = rec.get("model_id")
        after = rec.get("restore_after")
        if not mid or after is None:
            continue
        if ledger.now() < float(after):
            continue        # still coalescing; a new request may yet reuse it
        if _restore(mid, say):
            done.append(mid)
    return done


def restore_now(wait: bool = True) -> list[str]:
    """Bring back everything we released that nothing is using. Returns the ids.

    Exists because a short-lived caller exits before any deferred restore fires, and
    would otherwise leave a model down long after its work finished. Idempotent, and
    a no-op while advisory.
    """
    ids = []
    for rec in ledger.owed():
        mid = rec.get("model_id")
        if not mid:
            continue
        if _restore(mid, lambda _m: None, wait=wait):
            ids.append(mid)
    return ids


def snapshot() -> dict:
    """Read-only lease view for reports. **Decides nothing.**"""
    # Reap first: a remote holder that stopped renewing must not appear live, or it
    # would keep blocking the planner until something else happened to notice.
    with contextlib.suppress(Exception):
        reap_expired()
    led = ledger.snapshot()
    overdue = []
    limit = max_hold_s()
    if limit:
        now = ledger.now()
        for r in led["leases"]:
            age = now - float(r.get("started") or now)
            if age > limit:
                overdue.append({"lease_id": r.get("lease_id"), "pid": r.get("pid"),
                                "models": r.get("models"), "age_s": round(age)})
    return {"enforcing": enforcing(), "evicting": evicting(),
            "breaker": breaker.snapshot(), "cooldown_s": cooldown_s(),
            "leases": led["leases"], "lease_count": led["lease_count"],
            "owed": led["owed"], "overdue": overdue,
            "ledger": {k: led[k] for k in ("dir", "writable", "fstype", "schema")}}


# --- remote holders (another process, over HTTP) ----------------------------- #
# A local lease proves it is alive by holding a flock: the kernel drops it when the
# process dies, so liveness needs no timeout. A holder in ANOTHER process cannot do
# that through an HTTP call — the lock would belong to this process, not theirs — so a
# remote lease carries a deadline and must be renewed. Same guarantee, different
# mechanism: a client that dies stops renewing, and the lease lapses.
REMOTE_TTL_S = 300.0


POLL_AFTER_S = 1.5          # what a pending client is told to wait between polls


def acquire_api(model_id: str, *, reason: str = "", need_gib: float | None = None,
                priority: str | None = None, ttl_s: float | None = None,
                holder: str = "", wait: bool = True) -> dict:
    """Acquire on behalf of a remote caller. Returns a JSON-able verdict.

    `wait=False` is the shape a networked caller should use: the verdict comes back
    immediately with `state: "pending"` if a release is still running, and the client
    polls `state_api` until it is not. `wait=True` keeps the older behaviour of holding
    the call open until the room exists, and remains the default so a client that
    predates the poll endpoint is not silently told to start early.

    Blocking either way in the sense that matters to an event loop — it takes locks and
    writes files — so an async caller must still hand it to a worker thread.
    """
    say_lines: list[str] = []
    lease_obj, lid = _acquire(model_id, need_gib, reason, priority,
                              say_lines.append, defer=not wait)
    ttl = float(ttl_s or REMOTE_TTL_S)
    if lid:
        ledger.update_lease(lid, remote=True, holder=holder or "remote",
                            expires_at=ledger.now() + ttl, ttl_s=ttl)
    out = _verdict(lease_obj.model_id, lid, lease_obj.state, bool(lease_obj),
                   lease_obj.reason, list(lease_obj.released), lease_obj.enforced,
                   lease_obj.free_gib)
    out.update(ttl_s=ttl, log=say_lines)
    return out


def state_api(lease_id: str) -> dict:
    """Where a pending acquire has got to. Read-only; decides and actuates nothing.

    Distinguishes "still working" from "gone" deliberately. A lease that has vanished —
    reaped for not renewing, or lost with the process that was releasing for it — must
    not read as pending, or a client would poll forever for something nobody is doing.
    """
    rec = ledger.read_lease(lease_id)
    if not rec:
        return {"lease_id": lease_id, "state": "gone", "granted": False,
                "reason": "no such lease (expired, released, or never existed)",
                "released": [], "enforced": False, "free_gib": None}
    out = _verdict(
        (rec.get("models") or [""])[0], lease_id, str(rec.get("state") or ACTIVE),
        bool(rec.get("granted")), str(rec.get("verdict") or rec.get("reason") or ""),
        list(rec.get("released") or []), bool(rec.get("enforcing")),
        rec.get("free_gib"))
    out["log"] = list(rec.get("log") or [])
    if rec.get("error"):
        out["error"] = rec["error"]
    exp = rec.get("expires_at")
    if exp is not None:
        out["expires_in_s"] = round(float(exp) - ledger.now(), 1)
    return out


def _verdict(model_id: str, lid: str, state: str, granted: bool, reason: str,
             released: list, enforced: bool, free_gib: float | None) -> dict:
    """The wire shape, in one place so the acquire and the poll cannot drift apart."""
    out = {"lease_id": lid, "model": model_id, "state": state, "granted": granted,
           "ready": granted and state != PENDING, "reason": reason,
           "released": released, "enforced": enforced, "free_gib": free_gib}
    if state == PENDING:
        out["poll_after_s"] = POLL_AFTER_S
    return out


def heartbeat_api(lease_id: str, *, ttl_s: float | None = None) -> dict:
    """Renew a remote lease. A lapsed lease cannot be renewed — it is already gone."""
    rec = ledger.read_lease(lease_id)
    if not rec:
        return {"ok": False, "reason": "no such lease (expired or released)"}
    ttl = float(ttl_s or rec.get("ttl_s") or REMOTE_TTL_S)
    # Merge, never overwrite: the actuation thread may be writing progress into this
    # same record, and a heartbeat that clobbered it would strand a pending client.
    ledger.update_lease(lease_id, expires_at=ledger.now() + ttl)
    return {"ok": True, "expires_in_s": ttl,
            "state": str(rec.get("state") or ACTIVE)}


def release_api(lease_id: str) -> dict:
    """Release a remote lease and schedule any restore it made necessary."""
    rec = ledger.read_lease(lease_id) or {}
    models = rec.get("models") or []
    _release(lease_id, models[0] if models else "", lambda _m: None)
    return {"ok": True, "released": models}


def reap_expired() -> list[str]:
    """Drop remote leases whose holder stopped renewing. Returns the ids reaped.

    The counterpart to the kernel dropping a local holder's lock. Without it, a client
    that is killed mid-work would hold memory reserved forever.
    """
    gone = []
    for rec in ledger.live_leases():
        if not rec.get("remote"):
            continue
        exp = rec.get("expires_at")
        if exp is None or ledger.now() < float(exp):
            continue
        lid = rec.get("lease_id")
        models = rec.get("models") or []
        audit.record(kind="alloc.lease_expired", lease=lid, models=models,
                     holder=rec.get("holder"),
                     detail="remote holder stopped renewing")
        _release(lid, models[0] if models else "", lambda _m: None)
        gone.append(lid)
    return gone


# --- internals --------------------------------------------------------------- #
def _acquire(model_id, need_gib, reason, priority, say, *, defer=False):
    """Decide, record, and (only when enforcing) act. Never raises.

    With `defer=True` this returns as soon as the *verdict* is known and leaves any
    release to run on a worker thread, the lease reporting `PENDING` until it finishes.
    That is for a caller reaching the broker over HTTP, where holding a request open
    across a container stop makes the client's socket timeout — a number chosen with no
    knowledge of what it is waiting for — into a distributed-systems decision: give up
    too early and the client concludes nothing is coordinating, falls back to its own
    coordination, and now two components are acting on one container.

    A caller inside this process keeps the synchronous path. It wants the room before
    its body runs and has nothing useful to do until then, so waiting on the thread it
    already occupies is both simpler and exactly what it wants.
    """
    lid = ledger.new_lease_id()
    try:
        specs = spec.by_id()
        s = specs.get(model_id)
        if s is None:
            return Lease(model_id, True, f"{model_id!r} is not declared — not governed",
                         lease_id=""), ""
        want = need_gib if need_gib is not None else s.need_gib
        rank = spec._RANK.get((priority or s.priority), s.rank)
        free = capacity.free_gib()
        pl = policy.plan(model_id, need_gib=want, rank=rank, free_gib=free,
                         candidates=_candidates(specs, exclude=model_id),
                         speculative_max_s=spec.pool_config().get(
                             "speculative_max_s", 15.0))

        # Take the lease lock FIRST and keep it for the hold: its being held is what
        # makes this lease live, and the kernel dropping it is what makes a dead
        # holder detectable without any timeout.
        keeper = _LockKeeper(ledger.lease_lock_name(lid))
        if not keeper.take():
            _record("deny", model_id, lid, pl, note="lease lock unavailable")
            return Lease(model_id, True, "coordination unavailable — proceeding",
                         lease_id="", plan=pl, free_gib=free), ""
        _keepers[lid] = keeper
        granted = pl.admit or not enforcing()
        acting = bool(pl.steps) and evicting()
        state = PENDING if (acting and defer) else ACTIVE
        # `reason` stays the caller's ("board render"); the planner's verdict is a
        # separate field, because a poll has to report the decision and the two would
        # otherwise be confused for each other.
        ledger.write_lease(lid, {"models": [model_id], "reason": reason,
                                 "rank": rank, "need_gib": want,
                                 "enforcing": enforcing(), "state": state,
                                 "granted": granted, "verdict": pl.reason})

        released: tuple = ()
        if acting and defer:
            # The verdict is already final — `pl.admit` is computed from the plan, not
            # from its execution — so returning now tells the client the truth. What it
            # must still wait for is the room, which `state` reports.
            _spawn_actuation(lid, model_id, pl, say)
        elif acting:
            released = _execute(pl, say)
        elif pl.steps:
            why = ("eviction is off (alloc.lease.evict)" if enforcing()
                   else "advisory mode (alloc.lease.enforce)")
            say(f"{why}: would release {', '.join(pl.evicts) or 'nothing'} "
                f"for {model_id}")

        _record("grant" if pl.admit else "short", model_id, lid, pl,
                released=list(released), state=state)
        return Lease(model_id, granted, pl.reason, lease_id=lid,
                     plan=pl, released=released, free_gib=free,
                     enforced=enforcing(), state=state), lid
    except Exception as e:  # noqa: BLE001 — coordination must never break the work
        say(f"lease failed, proceeding uncoordinated: {e}")
        return Lease(model_id, True, f"allocator error, proceeding: {e}"), ""


def _release(lid, model_id, say):
    """Drop the hold; if nothing else holds what we released, schedule a restore."""
    keeper = _keepers.pop(lid, None)
    if keeper is not None:
        keeper.release()
    if lid:
        ledger.drop_lease(lid)
    # Derived, not decremented: ask the ledger who is still holding this model.
    if ledger.holders_of(model_id):
        return
    owed = [r.get("model_id") for r in ledger.owed() if r.get("model_id")]
    for mid in owed:
        ledger.set_model_state(mid, restore_after=ledger.now() + cooldown_s())
        say(f"idle — {mid} may be restored in {cooldown_s():.0f}s "
            "unless another request arrives")
    if owed:
        _arm_restore_timer(say)


def _arm_restore_timer(say) -> None:
    """Fire the due restores once the cooldown lapses, in this process.

    The prompt half of the pair described in `restore_due`. A single timer is kept and
    re-armed rather than one per model, so a burst does not stack them; it is a daemon
    thread so it can never hold up an exit, and the watchdog covers the case where this
    process is gone before it fires.
    """
    global _restore_timer
    delay = cooldown_s() + 1.0
    with _lock:
        if _restore_timer is not None and _restore_timer.is_alive():
            return          # one pending timer is enough; it re-reads the ledger
        t = threading.Timer(delay, _restore_timer_fire, args=(say,))
        t.daemon = True
        _restore_timer = t
        t.start()


def _restore_timer_fire(say) -> None:
    global _restore_timer
    try:
        restore_due(say)
    except Exception as e:  # noqa: BLE001 — a restore must never take the process down
        say(f"restore pass failed: {e}")
    finally:
        with _lock:
            _restore_timer = None


def _spawn_actuation(lid: str, model_id: str, pl: "policy.Plan", say) -> None:
    """Run a plan's releases on a worker thread, reporting progress into the lease.

    The lease record is the only channel here, because the caller that asked for this
    is at the other end of an HTTP request that has already been answered. It is also
    durable in the right way: if this process dies mid-release, the lease's lock dies
    with it and the whole lease is reclaimed, so no client can ever be left polling a
    record that nothing is working on.
    """
    def run() -> None:
        lines: list[str] = []
        try:
            released = _execute(pl, lines.append)
            ledger.update_lease(lid, state=ACTIVE, released=list(released),
                                log=lines, free_gib=capacity.free_gib())
            _record("actuated", model_id, lid, pl, released=list(released),
                    state=ACTIVE)
            for line in lines:
                say(line)
        except Exception as e:  # noqa: BLE001 — a release must not take the router down
            ledger.update_lease(lid, state=FAILED, error=str(e), log=lines)
            _record("actuate_failed", model_id, lid, pl, note=str(e), state=FAILED)
            say(f"release for {model_id} failed: {e}")
        finally:
            with _lock:
                _actuating.discard(t)

    t = threading.Thread(target=run, name=f"alloc-actuate-{model_id}", daemon=True)
    with _lock:
        _actuating.add(t)
    t.start()


def wait_for_actuations(timeout: float = 30.0) -> bool:
    """Wait for in-flight releases to finish. True if none remain.

    Two callers, one obvious and one not. A process shutting down uses it so a release
    is not abandoned midway; correctness does not depend on that — the lease's lock dies
    with the process and the watchdog converges — but finishing a `docker stop` cleanly
    is better than leaving it half-done.

    The unobvious one is tests. A release runs on a thread, so it can outlive the
    fixture that redirected the ledger path, and its next write then lands in the *real*
    ledger — which is how `victim` once appeared in a live box's state. A static guard
    cannot see that: the file patches the seam correctly and still leaks. Joining here is
    what makes the redirection cover the whole action.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _lock:
            alive = [t for t in _actuating if t.is_alive()]
        if not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        alive[0].join(timeout=min(0.2, max(0.0, deadline - time.monotonic())))


def _execute(pl: "policy.Plan", say) -> tuple:
    """Run a plan's steps, re-measuring after each. Only called when enforcing.

    Re-measuring matters: the plan's arithmetic is a hypothesis built from declared
    and sometimes estimated sizes, and the pool is the only fact. A step that frees
    less than predicted must change what happens next rather than being assumed.

    One actor at a time per model. Two requesters can plan to release the same model
    concurrently — more easily now that a deferred acquire answers immediately and then
    works in the background — and a duplicate release is not merely wasted work: a
    second `stop` landing while the first has moved on to a restore is how a model comes
    back up in the middle of the render that displaced it. So the model's lock is taken
    around the action. Failing to get it means the peer is already doing exactly what
    this step wanted, so the right response is to wait for *the memory*, not to repeat
    the action. The lock is held across the driver call; `pool.lock`, taken inside the
    breaker, stays a leaf.
    """
    done = []
    specs = spec.by_id()
    for step in pl.steps:
        s = specs.get(step.model_id)
        if s is None:
            continue
        drv = _driver(s)
        if drv.observe_only:
            continue
        try:
            with ledger.flock(_model_lock(step.model_id), wait_s=0.5):
                if _release_one(step, drv, say):
                    done.append(step.model_id)
        except ledger.LockBusy:
            say(f"{step.model_id} is already being released by another actor — "
                "waiting for its memory instead of repeating the action")
            capacity.wait_free(pl.need_gib, timeout=max(15.0, step.release_s))
        if pl.need_gib is not None:
            free = capacity.free_gib()
            if free is not None and free >= pl.need_gib:
                break       # enough already; do not spend the remaining steps
    return tuple(done)


def _model_lock(model_id: str) -> str:
    return os.path.join("models", f"{model_id}.lock")


def _release_one(step, drv, say) -> bool:
    """One release, under the model's lock. True if the memory came back."""
    # Every state-changing action passes the breaker and the global budget first,
    # so neither a broken model nor a thrashing allocator can spend without limit.
    ok, why = breaker.allowed(step.model_id)
    if not ok:
        say(f"skipping {step.model_id}: {why}")
        return False
    breaker.record_attempt(step.model_id)
    res = drv.release(step.mode, need_gib=step.expect_gib)
    say(f"{'released' if res.ok else 'could not release'} {step.model_id} "
        f"({step.mode.value}): {res.detail}")
    if not res.ok:
        breaker.record_failure(step.model_id, res.detail, cause="release_failed")
        return False
    breaker.record_success(step.model_id)
    ledger.set_model_state(step.model_id, released_by_us=True,
                           mode=step.mode.value, at=ledger.now())
    return True


def _restore(model_id: str, say, *, wait: bool = True) -> bool:
    """Bring one model back, if we are the reason it is down and it can fit.

    The memory check here is the actual cure for the restart storm. A supervisor that
    retries an impossible start forever does so because nothing ever told it the
    attempt could not succeed. So: if the pool is readable and short, **do not
    attempt**, record it as deferred rather than failed, and try again when conditions
    change. A model waiting for room is not a broken model, and must never be given up
    on as though it were.
    """
    if not enforcing():
        return False
    if not ledger.released_by_us(model_id):
        # Never start what we did not stop: an operator's deliberate shutdown, or
        # another supervisor's business.
        return False
    s = spec.by_id().get(model_id)
    if s is None:
        return False
    drv = _driver(s)
    if drv.observe_only:
        return False

    ok, why = breaker.allowed(model_id)
    if not ok:
        say(f"not restoring {model_id}: {why}")
        return False

    free = capacity.free_gib()
    need = s.need_gib
    if free is not None and need is not None and free < need:
        holders = [h.get("lease_id") for h in ledger.live_leases()]
        detail = (f"{free:.0f}GiB free < {need:.0f}GiB needed"
                  + (f"; {len(holders)} lease(s) hold the pool" if holders else ""))
        breaker.record_failure(model_id, detail, cause="insufficient_memory")
        say(f"deferring {model_id}: {detail}")
        return False

    breaker.record_attempt(model_id)
    res = drv.acquire(timeout=float((s.restore or {}).get("cold_s", 600) or 600))
    ledger.set_model_state(model_id, released_by_us=not res.ok,
                           restored_at=ledger.now())
    if res.ok:
        breaker.record_success(model_id)
    else:
        breaker.record_failure(model_id, res.detail, cause="start_failed")
    say(f"{'restored' if res.ok else 'could not restore'} {model_id}: {res.detail}")
    audit.record(kind="alloc.restore", model=model_id, ok=res.ok, detail=res.detail)
    return res.ok


def _candidates(specs: dict, *, exclude: str) -> list:
    """Assemble policy inputs from drivers and the ledger. The only glue layer."""
    out = []
    for mid, s in specs.items():
        if mid == exclude or not s.local:
            continue
        drv = _driver(s)
        res = drv.residency()
        rp = drv.plan_release()
        out.append(policy.Candidate(
            model_id=mid, rank=s.rank, pinned=s.pinned,
            resident=res.resident, gib=res.gib, measured=res.measured,
            options=rp.options, blocked=rp.blocked,
            held_live=bool(ledger.holders_of(mid)),
            last_used=float(ledger.model_state(mid).get("at") or 0.0)))
    return out


def _driver(s):
    ctx = DriverContext(
        model_id=s.id, weight_gb=s.weight_gb,
        config={**s.driver_config, "release": s.release, "restore": s.restore},
        readiness=s.readiness, free_gib=capacity.free_gib,
        wait_free=capacity.wait_free)
    return for_spec(s.driver, ctx)


_keepers: dict = {}


class _LockKeeper:
    """Holds a `flock` for a lease's whole life, across the caller's block.

    A context manager cannot span the yield of another context manager cleanly, so
    the descriptor is kept explicitly. Its being held is the lease's liveness proof.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._fd: int | None = None

    def take(self) -> bool:
        import fcntl
        path = ledger._path(self.name)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        import fcntl
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None


def _log_path() -> str:
    """Where decisions are recorded — a seam, deliberately.

    This log is the evidence an operator reads before allowing the allocator to act, so
    a test writing into it is not a cosmetic problem: it puts fabricated decisions about
    fixture models into the record that real ones are judged from. Patch this, the way
    `capacity._baseline_path` is patched, rather than the environment.
    """
    return os.path.join(settings.logs_dir(), _LOG_NAME)


def _record(event: str, model_id: str, lid: str, pl: "policy.Plan",
            released: list | None = None, note: str = "", state: str = "") -> None:
    """Append one decision to `logs/alloc.jsonl`.

    This log is the evidence for turning enforcement on: it shows what the allocator
    would have done, under real load, before it is allowed to do anything.
    """
    rec = {
        "ts": round(time.time(), 3), "event": event, "model": model_id,
        "lease_id": lid, "enforcing": enforcing(), "admit": pl.admit,
        "gated": pl.gated, "free_gib": pl.free_gib, "need_gib": pl.need_gib,
        "projected_gib": pl.projected_gib, "shortfall_gib": pl.shortfall_gib,
        "steps": [{"model": s.model_id, "mode": s.mode.value,
                   "expect_gib": s.expect_gib, "speculative": s.speculative,
                   "reason": s.reason} for s in pl.steps],
        "released": released or [], "reason": pl.reason, "note": note,
    }
    if state:
        rec["state"] = state
    try:
        import json
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break a render
        pass
