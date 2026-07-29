# Running two models on one box

Ava keeps one model resident. The moment you add a second heavy one — an image
pipeline, a video model, a voice sidecar on the GPU — you have a problem that has
nothing to do with either model working correctly: **they want the same memory, and
there is not enough for both.**

This page is about that. It is optional: with nothing declared, Ava behaves exactly
as it always has.

---

## The problem, concretely

Here are real numbers from the machine this was built on, a 121.7 GB unified-memory
box:

| | |
|---|---|
| Total pool | 121.7 GB |
| OS + everything else already running | ~33 GB |
| The language model, resident | 48.7 GB |
| One image render (FLUX.2 weights) | 66.5 GB |

Any two of those fit. All three never do — and no `--gpu-memory-utilization` setting
changes that, because the sum simply exceeds the machine.

What happens without coordination is worse than a clean failure:

- The render starts, takes the memory, and the model server's next restart finds
  none. Its startup check fails, its supervisor retries, and because nothing caps
  retries it does that **7,997 times** while nobody notices.
- Or the model server wins, and renders OOM-kill halfway through.
- Or both squeeze in, the kernel starts swapping, and everything gets slow in a way
  no single component looks responsible for.

If you only ever run one model, none of this applies to you.

---

## What Ava does about it

You **declare** which models may hold memory. Ava then checks whether something fits
*before* it starts, and frees room by releasing models you declared — never anything
you did not.

Three ideas carry the whole design:

**Declared, never discovered.** Only models under `alloc.models` are ever started,
stopped, or unloaded. Anything else holding memory is counted — so Ava never promises
memory someone else has — and named, so you can see it. It is never touched. The
blast radius is your config file, not a heuristic.

**Ask for what you need, not for someone else's memory.** A caller says *"I am about
to use this model"*. It never says *"stop that other thing"*. That inversion is why a
pipeline added later inherits coordination instead of having to remember it — the
failure that produced those 7,997 restarts was a new render path that simply did not
know it was supposed to pause anything.

**Readiness means resident.** A model server binds its port long before its weights
load, and a service manager reports "active" the instant the process starts. A model
that hit an out-of-memory error during warm-up, caught it, and served HTTP anyway is
indistinguishable from a healthy one to any liveness check. On the development box
one did exactly that for **six days** — its own `/health` said so the whole time,
because nothing read it. Ava reads it.

---

## Turning it on

### 1. Declare a model

In `ava.yaml`. The minimum is an id, roughly what it costs, and how to act on it:

```yaml
alloc:
  models:
    my-llm:
      driver: docker
      priority: interactive
      driver_config: { container: my-llm }
      readiness:
        url: "http://127.0.0.1:8000/v1/models"
        expect: models_contains
```

If that id matches an `inference.backends.<id>` you already configured, its
`weight_gb` / `min_free_gb` / `tier` are **inherited from the `fit:` block you already
wrote** — you do not restate them. Same for a connector's unit and health probe.

Check what Ava now sees:

```console
$ ava alloc status
Allocation
  mode          : advisory · eviction off
  pool          : 59.0 / 121.7 GB free (system-psutil) · 61.8 GB undeclared
  actions       : 0/20 in the last 10 min
  leases        : 0 held · 0 awaiting restore
  my-llm           docker       interactive  resident · 44.3 GB
```

### 2. Read the log before you let it act

Nothing is actuated yet. `alloc.lease.enforce` is **false** by default, and in that
mode Ava computes the full decision and writes it to `logs/alloc.jsonl` without
touching anything. Let that run under your real workload, then read it. `ava alloc
plan <model>` shows one decision in full:

```console
$ ava alloc plan my-render
Plan for my-render
  admit     : True
  need/free : 70 / 8 GB -> projected 102 GB
  try (cheap) : my-engine [unload] — cheap and reversible (8s to release, 0s to restore)
  release     : my-llm [stop] — same priority (rank 0) but idle — no live lease
```

Note the order: the engine's *unload* endpoint is tried first because it is cheap and
reversible, and only then is anything stopped.

### 3. Enable it, in two steps

There are two switches, not one, because the halves carry opposite risk. Waiting is at
worst slower; **releasing** something is at worst taking memory away from work in
progress.

```yaml
alloc:
  lease:
    enforce: true     # Ava may act: refuse a start that cannot fit, restore what it released
    evict: false      # ...but may NOT take memory away from anything yet
```

Run on `enforce` alone for a while. When you are satisfied, set `evict: true`.

---

## What it will and will not do

**It refuses to start what provably cannot fit.** This is the important one, and it is
the actual cure for a restart storm rather than a cap on its symptoms. A model waiting
for room is *deferred*, not failed — it never counts against the give-up budget, so it
stays retryable forever and returns the moment room appears. Measured with a
permanently-short pool: **zero start attempts** over ~56 simulated hours.

**It bounds the retries that do happen.** A genuinely broken model gets exponential
backoff and is given up on after 6 attempts or 30 minutes, with an alert naming the
command that clears it. Six attempts, not 7,997.

**It refuses to thrash.** A global budget caps state-changing actions across all
models and all processes. Exceed it and Ava **quiesces**: it stops actuating entirely
and every lease becomes advisory. Its failure mode is to become a no-op, never a loop.

**It will not preempt work in flight.** A live lease at your own priority or better is
never taken. Lower-priority holders do yield — that is what declaring a priority is
for — and a model that is merely resident and idle is fair game.

**It will not touch what it did not stop.** Ava restores only models it released
itself. A model you shut down deliberately stays down.

---

## On any hardware

Every memory reading goes through Ava's hardware layer, which knows what "free" means
where it is running:

| Your machine | What Ava reads |
|---|---|
| Discrete GPU | free VRAM |
| Unified memory (Apple Silicon, Grace-class) | the system pool — a device-memory query returns nothing there |
| CPU-only, or nothing readable | **unknown**, which means *never gate* |

Unknown never becomes zero. A box Ava cannot measure behaves exactly as it did before
this layer existed. A driver whose tooling is missing — no container runtime, no
service manager — degrades to observe-only: still reported, never actuated.

> **One number matters, and it is easy to get wrong.** Use `MemAvailable`, not
> `MemFree`. They differ by however much reclaimable page cache is holding recently-read
> model weights — on the development box, by ~40 GB. A monitor reading `MemFree` will
> tell you the machine is at 93% when it is at 59%, and several tools do read it,
> including some engines' own memory displays. `ava alloc status` always shows which
> source it used.

---

## Adding support for your engine

Built-in drivers cover a container, a service unit, and an HTTP unload endpoint. For
anything else, write one file in `$AVA_HOME/alloc_drivers/`:

```python
from ava_bridge.alloc.base import ModelDriver, ReleaseMode, Residency, ActionResult

class MyEngineDriver(ModelDriver):
    name = "myengine"                                   # what `driver:` selects
    RELEASE_MODES = (ReleaseMode.UNLOAD, ReleaseMode.STOP)

    def residency(self) -> Residency:
        """Is it holding memory, how much, and is it actually ready?"""
        gib = my_engine.resident_gib()                  # your control plane
        return Residency(resident=gib > 0, gib=gib, measured=True,
                         ready=my_engine.weights_loaded())

    def release(self, mode, *, need_gib=None, abort=None, timeout=180.0):
        """Give the memory back. Return only once the pool has ACTUALLY dropped."""
        before = self.ctx.free_gib()
        my_engine.stop() if mode is ReleaseMode.STOP else my_engine.unload()
        freed = self.ctx.wait_free((before or 0) + (need_gib or 0),
                                   timeout=timeout, abort=abort)
        after = self.ctx.free_gib()
        return ActionResult(ok=bool(freed), acted=True,
                            freed_gib=None if before is None else after - before)

    def acquire(self, *, abort=None, timeout=600.0):
        """Bring it back. REQUIRED whenever a release option is reversible."""
        my_engine.start()
        return ActionResult(ok=my_engine.wait_ready(timeout=timeout))

DRIVER = MyEngineDriver
```

Then `driver: myengine` in your model's block, and drop the file in
`$AVA_HOME/alloc_drivers/` (created for you by `ava setup`). Nothing in Ava's
core changes. If it does not load, `ava doctor` names the file and the reason —
a missing `DRIVER` symbol and a missing `name` are both reported, because both
would otherwise fail silently.

Three rules worth internalising, because each prevents a specific silent failure:

- **Implement `acquire()` if any release option is reversible.** The base class's
  no-op default exists for engines that reload themselves on next use, and it
  returns `ok=True` unconditionally — so a driver that stops something but does
  not override `acquire` gets marked restored without being restarted, and
  never comes back. If your engine genuinely reloads on demand, say so with
  `SELF_RESTORING = True` instead, and `validate()` will stop asking.
- **Report the measured delta, not the estimate**, and let `ok` be whatever
  `wait_free` returned. Memory reclaim is asynchronous; "we ran the stop
  command" and "the memory is back" are different facts, and only the second
  licenses starting something else. Use `acted=True` to say the thing IS down
  even when the pool did not move — that is what makes Ava owe you a restore
  rather than leaving it stopped forever.
- **`resident=None` means unknown and must never become `False`.** Memory you
  cannot see is memory the planner must not promise to free.

---

## When something is wrong

`ava doctor` has an Allocation section, and the watchdog raises alerts that stay
active while the condition persists and clear themselves when it is fixed:

| Alert | Meaning |
|---|---|
| `alloc_degraded_<model>` | **Running but no weights loaded.** The dangerous one — its port answers, so nothing else notices. |
| `alloc_absent_<model>` | Declared, but not installed here. Usually a typo, or config carried from another machine. |
| `alloc_unfit_<model>` | Has not been able to start for a sustained period. |
| `alloc_unknown_hog` | Undeclared processes holding memory while a declared model is blocked. |
| `alloc_giveup_<model>` / `alloc_quiesced` | Ava stopped trying, and why. Both name the command that resumes. |

```console
ava alloc status              # pool, leases, breaker, per-model residency
ava alloc plan <model>        # what would happen, actuating nothing
ava alloc restore             # bring back what Ava released
ava alloc reset <model>       # clear a give-up after you have fixed the cause
ava alloc resume              # un-quiesce
```

---

## Another application on the same box

A second app can hold leases without importing Ava — that is how two applications
stop fighting over one pool: exactly one component decides, and the other asks.

```
POST   /lease            {"model": "my-render", "reason": "render",
                          "ttl_s": 300, "wait": false}
         -> {"lease_id": "...", "state": "pending", "granted": true,
             "ready": false, "poll_after_s": 1.5}
GET    /lease/<id>       -> {"state": "active", "ready": true,
                             "released": ["my-llm"]}
POST   /lease/<id>/heartbeat
DELETE /lease/<id>
```

On Ava's router, token-guarded — these endpoints can stop and start models.

**Send `"wait": false` and poll.** The verdict is known immediately; the *room* is not,
because stopping a container and waiting for the kernel to hand its memory back takes
minutes. So the acquire answers at once with `state: pending`, makes the room on its own
thread, and you poll `GET /lease/<id>` until `ready`. Three states are terminal:
`active` (go), `failed` (a release errored — you are uncoordinated, not blocked), and
`gone` (the lease no longer exists).

The alternative — holding the request open — sounds simpler and is a trap worth
describing, because it cost a night here. It makes your HTTP socket timeout into a
policy decision, and that number was chosen with no knowledge of what it is waiting for.
When it fires mid-release the client concludes nothing is coordinating, falls back to
coordinating for itself, and now **two components are stopping the same container** —
the exact failure the broker exists to remove. Polling puts the waiting where it is
visible and where it knows what it is waiting for. (`"wait": true` is still the default,
so a client written before the poll endpoint existed is never told to start early.)

Two rules for a client, both of which are about that same hazard:

- **Distinguish "timed out" from "refused".** Refused means nothing is running, so
  coordinating locally is safe. A timeout means the broker may still be acting — so
  proceed *without* a local fallback rather than becoming the second actor.
- **Renew from the moment you acquire, not from the moment you are granted.** Making
  room can take longer than the TTL, and a lease that lapses while its own release is
  still running gets reaped out from under the work it was making room for.

A caller inside Ava proves it is alive by holding a file lock the kernel releases if it
dies; an HTTP caller cannot, so a remote lease carries a deadline instead. Stop renewing
and Ava reclaims it, which is what stops a killed client from holding memory reserved
forever.

The client is ~200 lines of standard library that the other app vendors, on the same
terms as any other cross-app file — the same shape as the device-side helper in
[`sdk/host/ava_device/`](../sdk/host/ava_device/).

---

## One operating rule

**Start a declared model through Ava, not by running its launch script directly.** A
raw `docker run` or `systemctl start` is a start with no lease and no fit check — Ava
would have refused it — and doing that on a busy box is how you oversubscribe the very
pool this exists to protect. Use `ava alloc restore`, the lease API, or a boot unit
that Ava knows about.

The launch script stays the *mechanism*. Ava is the *interface*.

> **Put the client's opt-in where the code is, not where the service manager is.** If
> the other app enables leasing from a systemd drop-in, an environment file, or a
> wrapper script, then anything launched another way — a batch script, a REPL, a cron
> job, a colleague's one-off — silently reverts to whatever it did before, and nothing
> anywhere records that it happened. Put the default in a module every entry point
> imports, and make opting *out* the explicit act. Coordination that only applies to
> processes started the blessed way is coordination you cannot rely on.

**Next step:** [Connect your apps](CONNECTOR_SDK.md) so the rest of your stack can ask
for memory the same way.
