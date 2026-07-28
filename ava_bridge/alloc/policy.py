"""The planner — decide what to release so a model can run. Pure, no I/O.

Given a request, the current pool, and what every declared model is holding, this
produces an ordered plan: which levers to pull, in what order, and whether the
result is expected to be enough. It performs no actuation, reads no hardware, opens
no sockets and consults no clock — every input arrives as an argument.

That purity is the point. Eviction policy is the part of an allocator most likely to
be wrong, most likely to need tuning per deployment, and hardest to test if it is
entangled with subprocess calls. Here it is a function over data: a fork that
disagrees with interactive-wins replaces `plan()` and nothing else, and the whole
decision table is exercised in milliseconds with no GPU.

**Admission first; release only when necessary.** The first question is whether the
request already fits, and if it does the plan is empty. This matters more than it
sounds: coordination schemes that pause a heavy model for *every* request pay the
full reload cost even when the box had room all along.

**Then the cheap, reversible levers, speculatively.** An engine that can drop its
weights in a few seconds and reload them on next use costs almost nothing to
release, so those are tried before anything is stopped — even when their yield is
unknown, because an unknown yield for near-zero cost is worth spending.

**Then eviction, by declared priority.** The rules, in order of application:

  1. never a model the operator pinned;
  2. never one that is not actually holding anything;
  3. never one whose driver cannot release it (observe-only, missing tooling, a
     model on another host);
  4. never **preempt a live lease at the requester's own rank or better** — that is
     how you corrupt work someone is in the middle of. An equal-rank model that is
     merely resident and *idle* is fair game;
  5. among what remains: lowest priority first, then cheapest to restore, then
     biggest yield, then least recently used.

Rule 4 is the one that keeps this safe to enable. Everything else is an efficiency
question; that one is a correctness question.

**Unknown never gates.** An unreadable pool yields an empty plan with `admit=True`:
a box we cannot measure must behave exactly as it did before this layer existed. An
unknown weight is the same — we cannot compute a shortfall, so we do not pretend to.

**Scope.** Decides. Does not measure (`capacity`), actuate (`drivers`), or sequence
and lock (`lease`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .base import ReleaseMode, ReleaseOption

# Cheap enough to try without knowing what it will yield. A lever this reversible
# costs about as much as measuring twice, so speculating is nearly free.
SPECULATIVE_MAX_RELEASE_S = 15.0
SPECULATIVE_MAX_RESTORE_S = 5.0


@dataclass
class Candidate:
    """One declared model considered as a source of memory.

    Assembled by the caller from a spec, a driver's residency, and its release plan,
    so this module never touches a driver. `held_live` is the crucial one: it means
    some caller currently holds a lease on this model and is presumably using it.
    """

    model_id: str
    rank: int                       # lower = higher priority (see spec.PRIORITIES)
    pinned: bool = False
    resident: bool | None = None
    gib: float | None = None        # what it is holding, if known
    measured: bool = False
    options: tuple[ReleaseOption, ...] = ()
    blocked: str | None = None      # non-None -> driver cannot release it, and why
    held_live: bool = False         # a live lease is using it right now
    last_used: float = 0.0          # for LRU tie-breaks; any monotonic-ish number

    @property
    def releasable(self) -> bool:
        return (not self.pinned and not self.blocked and bool(self.options)
                and self.resident is not False)


@dataclass
class Step:
    """One action the plan proposes."""

    model_id: str
    mode: ReleaseMode
    expect_gib: float | None        # None = unknown yield
    release_s: float = 0.0
    restore_s: float = 0.0
    speculative: bool = False       # tried for its cheapness, not its arithmetic
    reason: str = ""


@dataclass
class Plan:
    """What to do about one request.

    `admit` is the verdict *assuming every step succeeds*. It is a prediction, not a
    promise — the sequencer re-measures after each step, because arithmetic is the
    hypothesis and the pool is the fact.
    """

    model_id: str
    admit: bool
    need_gib: float | None = None
    free_gib: float | None = None
    projected_gib: float | None = None
    shortfall_gib: float = 0.0
    steps: tuple[Step, ...] = field(default_factory=tuple)
    reason: str = ""
    gated: bool = True              # False = memory unknown, decision not memory-based

    @property
    def evicts(self) -> tuple[str, ...]:
        return tuple(s.model_id for s in self.steps if not s.speculative)


def plan(model_id: str, *, need_gib: float | None, rank: int,
         free_gib: float | None, candidates: "list[Candidate] | None" = None,
         speculative_max_s: float = SPECULATIVE_MAX_RELEASE_S) -> Plan:
    """Decide how to make room for `model_id`. Pure: same inputs, same plan."""
    cands = list(candidates or [])

    # --- the two "do not gate" escapes, both deliberate ---------------------- #
    if free_gib is None:
        return Plan(model_id, admit=True, need_gib=need_gib, gated=False,
                    reason="memory unknown — not gated")
    if need_gib is None:
        return Plan(model_id, admit=True, free_gib=free_gib, gated=False,
                    reason=f"{model_id} weight unknown — not gated")

    # --- already fits: the plan is to do nothing ----------------------------- #
    if free_gib >= need_gib:
        return Plan(model_id, admit=True, need_gib=need_gib, free_gib=free_gib,
                    projected_gib=free_gib,
                    reason=f"{free_gib:.0f}GiB free ≥ {need_gib:.0f}GiB needed — "
                           "nothing to release")

    steps: list[Step] = []
    projected = free_gib

    # --- speculative pass: cheap and reversible, regardless of known yield --- #
    for c in cands:
        if not c.releasable or c.held_live:
            continue
        for opt in _cheap_options(c, speculative_max_s):
            steps.append(Step(
                model_id=c.model_id, mode=opt.mode, expect_gib=opt.frees_gib,
                release_s=opt.release_s, restore_s=opt.restore_s,
                speculative=True,
                reason=f"cheap and reversible ({opt.release_s:.0f}s to release, "
                       f"{opt.restore_s:.0f}s to restore)"))
            # An unknown yield contributes nothing to the arithmetic — it may still
            # be what tips the balance, which the sequencer discovers by measuring.
            if opt.frees_gib:
                projected += opt.frees_gib
            break  # one lever per model in this pass

    if projected >= need_gib:
        return Plan(model_id, admit=True, need_gib=need_gib, free_gib=free_gib,
                    projected_gib=round(projected, 2), steps=tuple(steps),
                    reason="cheap reversible releases are expected to be enough")

    # --- eviction pass, by priority ------------------------------------------ #
    planned = {s.model_id for s in steps}
    for c in _eviction_order(cands, rank):
        if c.model_id in planned:
            continue
        opt = _best_option(c)
        if opt is None:
            continue
        steps.append(Step(
            model_id=c.model_id, mode=opt.mode, expect_gib=opt.frees_gib,
            release_s=opt.release_s, restore_s=opt.restore_s,
            reason=_evict_reason(c, rank)))
        if opt.frees_gib:
            projected += opt.frees_gib
        if projected >= need_gib:
            break

    admit = projected >= need_gib
    short = 0.0 if admit else round(need_gib - projected, 2)
    return Plan(
        model_id=model_id, admit=admit, need_gib=need_gib, free_gib=free_gib,
        projected_gib=round(projected, 2), shortfall_gib=short,
        steps=tuple(steps), reason=_plan_reason(admit, free_gib, need_gib,
                                                projected, short, steps, cands))


def _cheap_options(c: Candidate, max_release_s: float) -> list[ReleaseOption]:
    """Levers cheap enough to pull on spec: fast to release, fast to undo.

    Requires the cost to have been **declared**. An undeclared cost arrives as 0.0,
    and treating that as "free" would speculatively stop a service whose real reload
    cost is minutes — the opposite of what this pass is for. Unknown cost therefore
    goes through the ordinary eviction path, where priority governs it.

    Also restricted to UNLOAD: keeping the process up and dropping its weights is the
    definitionally cheap point on the release axis, and the only one an engine can
    typically undo by itself on the next request.
    """
    return [o for o in c.options
            if o.reversible
            and o.costs_known
            and o.mode is ReleaseMode.UNLOAD
            and o.release_s <= max_release_s
            and o.restore_s <= SPECULATIVE_MAX_RESTORE_S]


def _best_option(c: Candidate) -> ReleaseOption | None:
    """The lever to use when evicting: cheapest to undo, then biggest yield.

    UNLOAD sorts ahead of STOP for the same model because it is defined to cost no
    more — keeping the engine up and dropping its weights is strictly the lighter
    action.
    """
    if not c.options:
        return None
    return sorted(c.options,
                  key=lambda o: (o.restore_s, -(o.frees_gib or 0.0),
                                 0 if o.mode is ReleaseMode.UNLOAD else 1))[0]


def _eligible(c: Candidate, rank: int) -> bool:
    """May a requester at `rank` take this model's memory?"""
    if not c.releasable:
        return False
    if c.held_live and c.rank <= rank:
        # Someone at our rank or better is USING it. Preempting that is how you
        # break work in flight; wait or go without instead.
        return False
    return c.rank > rank or (c.rank == rank and not c.held_live)


def _eviction_order(cands: "list[Candidate]", rank: int) -> "list[Candidate]":
    """Lowest priority first, then cheapest to restore, biggest yield, LRU."""
    eligible = [c for c in cands if _eligible(c, rank)]

    def key(c: Candidate):
        opt = _best_option(c)
        return (-c.rank,                          # lowest priority first
                opt.restore_s if opt else 0.0,    # cheapest to bring back
                -((opt.frees_gib if opt else 0.0) or 0.0),   # biggest yield
                c.last_used)                      # least recently used
    return sorted(eligible, key=key)


def _evict_reason(c: Candidate, rank: int) -> str:
    if c.rank > rank:
        return f"lower priority (rank {c.rank} > {rank})"
    return f"same priority (rank {c.rank}) but idle — no live lease"


def _plan_reason(admit, free, need, projected, short, steps, cands) -> str:
    if admit:
        return (f"{free:.0f}GiB free, {need:.0f}GiB needed; releasing "
                f"{len(steps)} model(s) is expected to reach {projected:.0f}GiB")
    blocked = [f"{c.model_id} ({c.blocked})" for c in cands if c.blocked]
    pinned = [c.model_id for c in cands if c.pinned and c.resident]
    busy = [c.model_id for c in cands if c.held_live]
    bits = [f"{free:.0f}GiB free < {need:.0f}GiB needed; "
            f"still {short:.0f}GiB short after releasing everything eligible"]
    if pinned:
        bits.append(f"pinned: {', '.join(pinned)}")
    if busy:
        bits.append(f"in use: {', '.join(busy)}")
    if blocked:
        bits.append(f"cannot release: {', '.join(blocked)}")
    return ". ".join(bits)
