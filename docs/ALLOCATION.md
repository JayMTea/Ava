# Running two models on one box

**Is this page for you?** Only if you run a second heavy model beside Ava's
brain - a larger model you keep for coding, a batch model, a voice model on the
GPU. Two models that each work perfectly will still fight, because they want the
same memory and there is not enough for both. The result is not a clean error.
It is a restart loop, work killed halfway, or a machine that just gets slow and
no single component looks responsible.

This page shows you how to declare which models may hold memory, so Ava checks
that something fits *before* it starts it. It is entirely optional. Declare
nothing and Ava behaves exactly as it always has.

Reading time about ten minutes. You will not have to change how you launch
anything, only how you tell Ava about it.

---

## The problem, concretely

Here are real numbers from the machine this was built on, a 121.7 GB unified-memory
box:

| | |
|---|---|
| Total pool | 121.7 GB |
| OS + everything else already running | ~33 GB |
| The language model you chat to, resident | 48.7 GB |
| A second model, weights held at the same time | 66.5 GB |

Any two of those fit. All three never do - and no `--gpu-memory-utilization` setting
changes that, because the sum simply exceeds the machine.

What happens without coordination is worse than a clean failure:

- The second model loads, takes the memory, and the first server's next restart
  finds none. Its startup check fails, its supervisor retries, and because nothing
  caps retries it does that **7,997 times** while nobody notices.
- Or the first server wins, and the second is OOM-killed part-way through loading.
- Or both squeeze in, the kernel starts swapping, and everything gets slow in a way
  no single component looks responsible for.

If you only ever run one model, none of this applies to you.

---

## What Ava does about it

You **declare** which models may hold memory. Ava then checks whether something fits
*before* it starts, and frees room by releasing models you declared - never anything
you did not.

Three ideas carry the whole design:

**Declared, never discovered.** Only models under `alloc.models` are ever started,
stopped, or unloaded. Anything else holding memory is counted - so Ava never promises
memory someone else has - and named, so you can see it. It is never touched. The
blast radius is your config file, not a heuristic.

**Ask for what you need, not for someone else's memory.** A caller says *"I am about
to use this model"*. It never says *"stop that other thing"*. That inversion is why a
model added later inherits coordination instead of having to remember it - the
failure that produced those 7,997 restarts was a new load path that simply did not
know it was supposed to pause anything.

**Readiness means resident.** A model server binds its port long before its weights
load, and a service manager reports "active" the instant the process starts. A model
that hit an out-of-memory error during warm-up, caught it, and served HTTP anyway is
indistinguishable from a healthy one to any liveness check. On the development box
one did exactly that for **six days** - its own `/health` said so the whole time,
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
wrote** - you do not restate them. Same for a connector's unit and health probe.

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
$ ava alloc plan my-sidecar
Plan for my-sidecar
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
for room is *deferred*, not failed - it never counts against the give-up budget, so it
stays retryable forever and returns the moment room appears. Measured with a
permanently-short pool: **zero start attempts** over ~56 simulated hours.

**It bounds the retries that do happen.** A genuinely broken model gets exponential
backoff and is given up on after 6 attempts or 30 minutes, with an alert naming the
command that clears it. Six attempts, not 7,997.

**It refuses to thrash.** A global budget caps state-changing actions across all
models and all processes. Exceed it and Ava **quiesces**: it stops actuating entirely
and every lease becomes advisory. Its failure mode is to become a no-op, never a loop.

**It will not preempt work in flight.** A live lease at your own priority or better is
never taken. Lower-priority holders do yield - that is what declaring a priority is
for - and a model that is merely resident and idle is fair game.

**It will not touch what it did not stop.** Ava restores only models it released
itself. A model you shut down deliberately stays down.

---

## Freeing memory yourself

Everything above is about Ava deciding. This is about you deciding, and it is a
different thing with different rules.

From a terminal:

```
ava alloc status                 # what is holding memory
ava alloc release <model-id>     # free it
ava alloc restore <model-id>     # bring it back
```

The panel section that does this from the UI (**Memory Ava can free**) is written
and tested, but it is not wired in: the allocator underneath it is not yet
something to put behind a button an owner can press by accident. Until it is, the
terminal is the way.

Four things are worth knowing before you press it.

**It works whether or not enforcement is on.** `alloc.lease.enforce` and
`alloc.lease.evict` govern whether Ava may act *on its own judgement*. You pressing a
button is not Ava's judgement, so neither switch is consulted. This is why the control
does something on a fresh install, where both default to false.

**Nothing will put it back behind you.** A model you free is recorded as *yours*, not
as something Ava released - so the restore timer skips it, the watchdog skips it, and
it raises no alert about being down. It stays exactly where you put it until you bring
it back. That also means it is on you to bring it back.

**It will not interrupt work.** A release is refused while a live lease holds the
model, and while Ava is mid-answer. There is no force option, deliberately.

**You will be told what actually happened, including when that is nothing.** The
receipt reports a *measured* delta or admits it could not measure one. It will never
say "freed 0 GB", because that states an outcome where there was none. On a box whose
GPU no reader can reach - a container without the toolkit, WSL2 without the right
driver - freeing VRAM does not move the free-memory reading at all, and Ava says so
rather than counting a working release as a failure.

### What has a lever, and what does not

| Row | Lever | Comes back |
|---|---|---|
| Ava's own voice models | always | by itself, on the next voice turn |
| An engine that publishes an unload endpoint | inferred, if `alloc.infer_levers` | by itself, on the next request |
| A model you declared with `driver: docker` / `systemd` | yours | `Load it back` starts it again |
| A model you declared `pinned: true` | **none, by your choice** - the planner will not evict it and your own release is refused | - |
| Anything else | none - reported, never touched | - |

Ava fills in **one** kind of lever for you: an engine's own documented "drop your
weights" endpoint, aimed at a backend you already configured. That is bounded on
purpose - the address is one you wrote, the action is reversible, and a wrong guess is
a 404 that reads as "nothing was done". Ava will **never** infer a container or unit
name, because stopping the wrong process is not a mistake it can take back. Set
`alloc.infer_levers: false` to require an explicit `driver:` for everything.

If a row says **no release lever**, give it one:

```yaml
alloc:
  models:
    my-engine:
      driver: docker                    # it runs in a container
      driver_config: { container: my-engine }
```

```yaml
alloc:
  models:
    my-engine:
      driver: http-unload               # it answers a "drop your weights" request
      driver_config:
        base: "http://127.0.0.1:11434"  # the server ROOT, not the /v1 base
        path: /api/generate
        json: { model: llama3.2:3b, prompt: "", keep_alive: 0 }
```

The first stops a process, so Ava has to start it again. The second is reversible for
free - the engine reloads on the next request.

---

## On any hardware

Every memory reading goes through Ava's hardware layer, which knows what "free" means
where it is running:

| Your machine | What Ava reads |
|---|---|
| Discrete GPU | free VRAM |
| Unified memory (Apple Silicon, Grace-class) | the system pool - a device-memory query returns nothing there |
| CPU-only, or nothing readable | **unknown**, which means *never gate* |

Unknown never becomes zero. A box Ava cannot measure behaves exactly as it did before
this layer existed. A driver whose tooling is missing - no container runtime, no
service manager - degrades to observe-only: still reported, never actuated.

!!! note "One number matters, and it is easy to get wrong"

    Use `MemAvailable`, not `MemFree`. They differ by however much reclaimable
    page cache is holding recently-read model weights - on the development box,
    by ~40 GB. A monitor reading `MemFree` will tell you the machine is at 93%
    when it is at 59%, and several tools do read it, including some engines' own
    memory displays. `ava alloc status` always shows which source it used.

---

## Adding support for your engine

Built-in drivers cover a container, a service unit, and an HTTP unload endpoint. If
yours is one of those, you are done - skip this section.

Write one file in `$AVA_HOME/alloc_drivers/`:

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
core changes. If it does not load, `ava doctor` names the file and the reason -
a missing `DRIVER` symbol and a missing `name` are both reported, because both
would otherwise fail silently.

Three rules worth internalising, because each prevents a specific silent failure:

- **Implement `acquire()` if any release option is reversible.** The base class's
  no-op default exists for engines that reload themselves on next use, and it
  returns `ok=True` unconditionally - so a driver that stops something but does
  not override `acquire` gets marked restored without being restarted, and
  never comes back. If your engine genuinely reloads on demand, say so with
  `SELF_RESTORING = True` instead, and `validate()` will stop asking.
- **Report the measured delta, not the estimate**, and let `ok` be whatever
  `wait_free` returned. Memory reclaim is asynchronous; "we ran the stop
  command" and "the memory is back" are different facts, and only the second
  licenses starting something else. Use `acted=True` to say the thing IS down
  even when the pool did not move - that is what makes Ava owe you a restore
  rather than leaving it stopped forever.
- **`resident=None` means unknown and must never become `False`.** Memory you
  cannot see is memory the planner must not promise to free.

---

## When something is wrong

`ava doctor` has an Allocation section, and the watchdog raises alerts that stay
active while the condition persists and clear themselves when it is fixed:

| Alert | Meaning |
|---|---|
| `alloc_degraded_<model>` | **Running but no weights loaded.** The dangerous one - its port answers, so nothing else notices. |
| `alloc_absent_<model>` | Declared, but not installed here. Usually a typo, or config carried from another machine. |
| `alloc_unfit_<model>` | Has not been able to start for a sustained period. |
| `alloc_unknown_hog` | Undeclared processes holding memory while a declared model is blocked. |
| `alloc_config_<model>` | The driver's own `validate()` complaint about your declaration - an unbounded container restart policy that will fight the allocator, or no `readiness.url`, which makes `alloc_degraded` undetectable. |
| `alloc_giveup_<model>` / `alloc_quiesced` | Ava stopped trying, and why. Both name the command that resumes. |

A **pinned** model raises `alloc_degraded_<model>` like any other - a model that
is up and lying about its health is exactly what you want to hear about - but
never `alloc_unfit_<model>`, and it is never named as "blocked" by
`alloc_unknown_hog`. Ava cannot start it, so reporting that it has not started is
a complaint about somebody else's decision.

```console
ava alloc status              # pool, leases, breaker, per-model residency
ava alloc plan <model>        # what would happen, actuating nothing
ava alloc restore             # bring back what Ava released
ava alloc reset <model>       # clear a give-up after you have fixed the cause
ava alloc resume              # un-quiesce
```

---

## Another application on the same box

A second app can hold leases without importing Ava - that is how two applications
stop fighting over one pool: exactly one component decides, and the other asks.

### A model that belongs to another app

Before any of that, there is a smaller and more common case: another app on this
box is holding several GB, and Ava has no idea what it is. Undeclared, that memory
lands in the pool's **unknown** residual, which is what drives
`alloc_unknown_hog` - so Ava reports "something is holding 12 GB and I cannot say
what" while the answer is a perfectly well-behaved neighbour.

Declaring it is about **counting**, not controlling:

```yaml
alloc:
  models:
    their-drafter:
      label: Sidecar app's drafting model
      driver: docker
      pinned: true
      weight_gb: 11.6                    # MEASURED, not quoted
      driver_config: {container: their-drafter-llm}
      readiness:
        url: ${THEIR_APP_API:-http://127.0.0.1:8003}/v1/models
        expect: models_contains
        model: their-alias                # the SERVED alias, not the checkpoint
```

**`pinned: true` means never released, at any priority.** It is not a priority
level - it removes the model from candidacy entirely. The surprising half, and
the reason it is spelled out here: **your own `ava alloc release` is refused
too**, with the code `pinned`. That is deliberate. There is no force flag.

Reach for it when the other app treats the model as always-resident **and its own
supervisor would not bring it back**. That combination is what makes a release
unrecoverable: a container started `--restart no` by a `Type=oneshot` unit has
nobody watching it, so a model Ava stops stays stopped, and the other app's
feature is silently down until a human notices. Pinning is Ava declining a lever
whose consequences it cannot own.

What you still get, which is the whole point: the weight moves out of `unknown`
into `declared`, so the planner stops promising room that is not there; residency
is measured through the driver; and the readiness probe watches a model nothing
else on the box is watching.

!!! warning "The served-alias trap"

    For an OpenAI-compatible server, `expect: models_contains` must name the id
    that `/v1/models` **actually returns**. An engine started with
    `--served-model-name foo` answers `{"id": "foo", "root": "org/Checkpoint"}` -
    so matching on `org/Checkpoint` is False forever, which the watchdog reads as
    `degraded` and raises a **critical** alert about a completely healthy model.

    Getting this wrong is worse than omitting `expect` - but omitting it is not
    safe either: with no `require`/`expect`, **any** 2xx counts as ready, and a
    bare `/health` cannot tell a loaded model from a warm-up that hit OOM, caught
    it, and served its port anyway. That is the failure this whole layer exists
    for.

Applying it: `alloc.models` is read at boot. Edit `ava.yaml` and restart Ava. No
Setup screen writes this block, so no banner will appear to remind you.

??? note "The lease API (four endpoints, on Ava's router, token-guarded)"

    These endpoints can stop and start models, so they require the router token.

    ```
    POST   /lease            {"model": "my-sidecar", "reason": "batch job",
                              "ttl_s": 300, "wait": false}
             -> {"lease_id": "...", "state": "pending", "granted": true,
                 "ready": false, "poll_after_s": 1.5}
    GET    /lease/<id>       -> {"state": "active", "ready": true,
                                 "released": ["my-llm"]}
    POST   /lease/<id>/heartbeat
    DELETE /lease/<id>
    ```

    **Send `"wait": false` and poll.** The verdict is known immediately; the
    *room* is not, because stopping a container and waiting for the kernel to
    hand its memory back takes minutes. So the acquire answers at once with
    `state: pending`, makes the room on its own thread, and you poll
    `GET /lease/<id>` until `ready`. Three states are terminal: `active` (go),
    `failed` (a release errored - you are uncoordinated, not blocked), and
    `gone` (the lease no longer exists).

    A caller inside Ava proves it is alive by holding a file lock the kernel
    releases if it dies; an HTTP caller cannot, so a remote lease carries a
    deadline instead. Stop renewing and Ava reclaims it, which is what stops a
    killed client from holding memory reserved forever.

    The client is ~200 lines of standard library that the other app vendors, on
    the same terms as any other cross-app file - the same shape as the
    device-side helper in [`sdk/host/ava_device/`](../sdk/host/ava_device/).

**Two rules for a client**, both about the same hazard - becoming a second actor:

- **Distinguish "timed out" from "refused".** Refused means nothing is running, so
  coordinating locally is safe. A timeout means the broker may still be acting - so
  proceed *without* a local fallback rather than becoming the second actor.
- **Renew from the moment you acquire, not from the moment you are granted.** Making
  room can take longer than the TTL, and a lease that lapses while its own release is
  still running gets reaped out from under the work it was making room for.

??? note "Why polling, and not a request held open (a night it cost)"

    Holding the request open sounds simpler and is a trap. It makes your HTTP
    socket timeout into a policy decision, and that number was chosen with no
    knowledge of what it is waiting for. When it fires mid-release the client
    concludes nothing is coordinating, falls back to coordinating for itself,
    and now **two components are stopping the same container** - the exact
    failure the broker exists to remove. Polling puts the waiting where it is
    visible and where it knows what it is waiting for.

    (`"wait": true` is still the default, so a client written before the poll
    endpoint existed is never told to start early.)

---

## One operating rule

**Start a declared model through Ava, not by running its launch script directly.** A
raw `docker run` or `systemctl start` is a start with no lease and no fit check - Ava
would have refused it - and doing that on a busy box is how you oversubscribe the very
pool this exists to protect. Use `ava alloc restore`, the lease API, or a boot unit
that Ava knows about.

The launch script stays the *mechanism*. Ava is the *interface*.

??? note "Put the client's opt-in where the code is, not where the service manager is"

    If the other app enables leasing from a systemd drop-in, an environment
    file, or a wrapper script, then anything launched another way - a batch
    script, a REPL, a cron job, a colleague's one-off - silently reverts to
    whatever it did before, and nothing anywhere records that it happened. Put
    the default in a module every entry point imports, and make opting *out* the
    explicit act. Coordination that only applies to processes started the
    blessed way is coordination you cannot rely on.

---

## Where to next

- **Wire the rest of your stack in** so it can ask for memory the same way:
  [Connector SDK](CONNECTOR_SDK.md).
- **Choosing the model that holds that memory in the first place:**
  [Pick a model](CHOOSE_A_MODEL.md).
