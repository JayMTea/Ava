# 0005. A lease broker owns memory allocation across declared models

- **Status:** Accepted
- **Date:** 2026-07-25
- **Deciders:** project owner

## Context

Ava can sense hardware and decide what fits, but nothing acts on the decision.
`ava_bridge/hwinfo.py` reads the pool, `ava_bridge/model_fit.py` ranks backends
against it — and says so about itself: *"it does not launch, size, or quantize
engines (nothing in this repo does — the vLLM servers are pre-started)."*

That gap has a cost. `model_fit.fits_now(profile, free_gb, assume_loaded=False)`
is a complete cold-load admission predicate: implemented, unit-tested, and served
on `GET /fit` as `cold_load_ok`. It has **zero consumers**. Nothing can act on it.

Meanwhile the boxes that run Ava alongside a heavy image/video pipeline
oversubscribe their memory. A model server and a latent pipeline pipeline each want tens
of GiB of the same pool, so at most one can be resident. Operators have been
solving this per-application, and the failure modes are severe and quiet:

- An engine supervised by `--restart unless-stopped` with no backoff cap retries a
  start that cannot possibly succeed. Observed on the development box: **7,997
  restarts**, because a new render pipeline did not inherit a coordination hold
  that lived at call sites rather than at the boundary every path crosses.
- A CUDA model that OOMs during warm-up, catches the exception, and lets its HTTP
  server bind anyway is **indistinguishable from healthy** to `systemctl
  is-active` and to a liveness probe. Observed: a voice sidecar silently degraded
  to its CPU fallback for six days. Its own `/health` reported the truth the whole
  time; nothing polled it.
- The number most engines expose for "free memory" is the wrong one. On unified
  memory `nvidia-smi --query-gpu=memory.free` is unavailable, and a pipeline that
  caches weights in a CUDA async pool can report ~0 GiB reserved while holding
  tens of GiB. Deciding on the wrong number produces confident, wrong answers.

These are not one operator's problems. Any fork that adds a second model to a
single box inherits all three, and today Ava offers no mechanism to address them.

## Decision

Add `ava_bridge/alloc/`: a **lease broker over declared models**.

1. **Declared, not discovered.** The broker actuates only models the operator
   declared under `alloc.models` in `ava.yaml`. Undeclared processes are observed
   and accounted (so the planner does not promise memory they hold) but **never
   started, stopped, or unloaded**. Blast radius is bounded by configuration.
2. **Leases, not global switches.** A caller states its *need* (`alloc.lease("x")`)
   and never names a victim. Admission is checked first via the existing
   `model_fit.fits_now(..., assume_loaded=False)`; something is released only when
   the request genuinely does not fit.
3. **Per-model declared priority**, defaulting to interactive-wins /
   batch-queues. Policy lives in config, not in code, and is a pure function so a
   fork can replace it wholesale.
4. **State is derived, never maintained.** The refcount is the number of held
   lease locks; "is this model paused" is a driver probe; "who owns this eviction"
   is who holds that model's `flock`. The kernel releasing locks on process death
   *is* the crash-recovery mechanism, so no timeout heuristic is needed to detect
   a dead holder — and two owners of one eviction becomes unrepresentable.
5. **Library core plus optional daemon.** The importable core is complete on its
   own; the periodic reconciler holds no authority and runs in-process. Nothing
   new to install.
6. **The allocator may deny, degrade, or no-op — never block, loop, or lie.** A
   global action budget quiesces it into a pure no-op if it ever thrashes.
7. **A remote acquire answers before the room exists.** `POST /lease` with
   `wait: false` returns the verdict — known immediately, since admission comes from
   the plan rather than from executing it — as `state: pending`, runs the release on
   a worker thread, and the caller polls `GET /lease/<id>`. Holding the request open
   instead delegates the question *"has coordination failed?"* to the client's socket
   timeout, a number chosen with no knowledge of what it is waiting for; when it fires
   mid-release the client falls back to coordinating for itself and becomes a second
   actor on the same container. Waiting belongs on the client, as an explicit
   deadline. Observed live before it was fixed.

Portability is a hard requirement, not a later concern. All memory reads go
through the `hwinfo` HAL; unreadable memory means *unknown*, which means **do not
gate** — never zero. An absent `alloc:` block means every lease is granted and
nothing is ever stopped, i.e. exactly the behaviour that shipped before this
layer. A fork on a discrete GPU, on Apple Silicon, on CPU only, or without a
container runtime gets correct-but-coarser behaviour, never a regression.

## Consequences

### Positive
- `cold_load_ok` gains a consumer; the admission check finally runs before a load.
- A model that cannot fit is **not started**, which removes the restart-storm
  failure mode at its root rather than capping its symptom.
- Readiness means *resident*, so a warm-up OOM surfaces in one probe interval
  instead of never.
- Coordination moves from N call sites to the boundary every GPU path crosses, so
  a new pipeline inherits it by construction and a static guard fails a build that
  routes around it.
- One declaration replaces per-application env flags, ending the class of bug
  where two processes express the same physical decision with different knobs.
- Forks get memory management as a product feature: declare a model, get
  admission, eviction, restore, and honest health.

### Negative / trade-offs
- A new subsystem to maintain, and a new config surface to document.
- Cross-process coordination through a lock directory constrains deployment: the
  participating processes must agree on one ledger path, and network filesystems
  cannot provide the `flock` semantics the design relies on. Both are detected and
  reported rather than assumed.
- Actuation is convergent, not transactional. A successor to a dead actor
  re-probes and converges rather than resuming a half-finished action — correct,
  but it means an action may be attempted more than once.
- Releasing a model has a restore cost, so a poorly-declared priority can trade
  throughput for latency. Mitigated by coalescing and an adaptive cooldown.

### Neutral / follow-ups
- In-process lazy singletons are declared and accounted but not actuated; giving
  them release levers is deliberately out of scope for v1.
- `hwinfo.py` still requires three edit sites to add an accelerator, and its
  docstring references a provider class that does not exist. A
  `hwinfo_providers/` refactor would make that one file, and is tracked
  separately — the alloc layer depends on exactly one hardware function, so it
  does not block.
- `RouterState.ordered_backends()` unconditionally re-pins the configured primary
  to the front, which silently defeats a shed verdict. A real bug, but on the
  serving path and unrelated to memory; it gets its own change.

## Alternatives considered

- **Advisory only — compute fit, report, never act.** Zero risk, but leaves the
  operator monitoring memory by hand, which is the problem.
- **Full lifecycle owner — the allocator is the only thing allowed to start a
  model.** Maximum control, but it becomes a single point of failure for the box
  booting at all, and it cannot govern models it did not launch.
- **Per-application coordination** (the status quo: each app pauses the engine it
  knows about). Rejected: it is exactly how the restart storm happened — policy at
  call sites is opt-out-by-default for any new call site, and two applications
  cannot agree on who owns the pause.
- **Cap the engine's memory fraction so everything cofits.** Necessary hygiene and
  worth doing, but insufficient: when the two resident sets exceed the pool, no
  fraction exists that fits both. Measured on the development box, they exceed it
  by ~30%.
- **A separate supervisor service** (systemd/k8s-style). Heavier to install and
  operate, duplicates process supervision the platform already provides, and still
  needs the same admission logic underneath.
