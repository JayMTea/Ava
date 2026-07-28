# Data, memory & privacy

A hosted assistant can tell you it respects your privacy. It cannot show you
the files. Ava can: the **Data** page is a live inventory of every store on
disk — named, sized, described, and pointed at its exact path — plus the
controls to read, export and delete each one.

Nothing on this page is a promise about a remote system. It is a listing of
what is on your own disk right now.

## One data root

Everything Ava persists resolves under **`AVA_HOME`**. On a default bare-metal
install `AVA_HOME` is *the code root itself* (`ava_bridge/settings.py`), so a
fresh clone ends up like this:

```
ava/                    <- the checkout, and AVA_HOME
├── ava.yaml            <- your settings
├── data/               <- memory.db, chats.db, session key, tokens, password
├── logs/               <- audit ledger, performance, hardware history, devices
├── media/              <- gen/ (generated images), uploads/ (files you shared)
└── secrets/            <- backend API keys, router token
```

That is a deliberate default — the single-user layout is `./data ./logs
./media` and nothing has to be configured for it to work — but it is worth
saying plainly rather than implying a clean code/data split: **out of the box
your data lives inside the checkout.** Point `AVA_HOME` somewhere else (an
env var, or the compose volume, which mounts `./ava-data`) and every path
above moves with it; individual dirs can also be redirected with
`paths.data`, `paths.logs`, `paths.media`, `paths.uploads`, `paths.secrets`.

The upside of one root is that backup is trivial, and **Data → Maintenance →
Backup** says so in the app: it prints the resolved `AVA_HOME` and the total
bytes across every store. Copy that folder and you have copied everything —
memories, chats, config and keys. Restore by pointing `AVA_HOME` at the copy.

## The inventory (Data → Overview)

`GET /api/data/stores` walks the real filesystem and returns one entry per
store. The Overview tab renders each as a row with a **format badge**
(`SQLITE` / `JSON` / `JSONL` / `FILES` / `LOCKED`), an owner-facing
description of what it holds, the **`AVA_HOME`-relative path** as literal
code, **bytes on disk**, an **item count**, and **when it was last written**.
Stores with automatic retention carry an `auto-managed` badge; the secrets row
carries `never leaves this machine`. The panel subtitle totals it: *N stores ·
X on disk · metrics kept 183 days*.

| Store | Format | What it holds |
|---|---|---|
| **Memory** | `data/memory.db`, SQLite | Distilled facts + indexed document chunks; row shows facts / doc chunks / pinned counts |
| **Chats** | `data/chats.db`, SQLite | Every conversation; row shows chat count and total messages |
| **Audit ledger** | `logs/audit.jsonl` | The flight recorder — turns, recalls, memory edits, grants, self-edits |
| **Performance** | `logs/performance.jsonl` + rotated segments + `logs/rollups/` | Per-generation throughput, latency and energy, plus the hourly/daily rollups behind Vitals |
| **Allocation decisions** | `logs/alloc.jsonl` | What was asked for, what the pool held, what was released (row appears once the file exists) |
| **Hardware history** | `logs/hw_history/` | Minute- and hour-resolution GPU/memory/CPU samples |
| **Device events** | `logs/devices/` | One rotated JSONL stream per connector; counted in *streams*, not lines |
| **Generated media** | `media/out/` | Images and video Ava rendered |
| **Uploads** | `media/uploads/` | Files you shared in chat; the originals stay here after their text is indexed |
| **Secrets & keys** | `data/` + `secrets/` | Session signing key, internal tokens, login password, backend API keys |

Two properties make this an inventory you can trust rather than a dashboard
you have to take on faith:

**The Data API never returns file contents.** The module says so at the top —
browsing and editing go through the existing governed surfaces
(`/api/hub/memory*`, `/api/chats*`); `/api/data/*` returns sizes, counts and
timestamps. The only reads of actual records are the whitelisted log tails
below.

**Secrets are listed by name and purpose only.** The secrets row expands into
`name — purpose` pairs so you know exactly what is held, and the values are
never opened, displayed, or exported. The purposes are the honest ones the
code actually ships:

```
.secret          Session signing key
.internal_token  Internal API token
auth_password    Login password (stored 0600, not hashed)
router_token     Internal router token
```

That last label is the tone the whole page is written in. The source comment
next to it explains why it reads that way: `auth.set_password` writes the
value verbatim at mode `0600`, and *saying "hashed" in a security inventory
overstated the protection*. See [Security model](../../SECURITY.md) for the
full secret inventory and what each one gates.

## Chats (Data → Chats)

One row per conversation — title, message count, JSON weight, last update —
newest first, from `GET /api/data/chats`.

- **JSON** — `GET /api/data/chats/{id}/export`, the conversation verbatim.
- **MD** — `?format=md`, a readable Markdown transcript with timestamps,
  generated-image links and attachment names.
- **Delete** — `DELETE /api/chats/{id}`, behind a confirm that tells you what
  it is about to do. It is permanent, and it is **recorded in the audit
  ledger** as a `chat_delete` event, so the deletion itself leaves a trace
  even though the conversation does not.

## Logs (Data → Logs)

Tails of the append-only stores, newest first, straight from the files under
`logs/`: `GET /api/data/logs/{name}/tail?n=&kind=`. The name is a fixed
whitelist — `audit`, `performance`, `devices` — there is no arbitrary-path
read here. The UI pulls the last 100 records and refreshes every 15 s;
the route caps `n` at 500.

On the **Audit** source, sub-filters narrow by event kind: **All · Turns ·
Recalls · Memory edits · Grants · Chat deletes** (`turn`, `memory_recall`,
`memory_edit`, `grant`, `chat_delete`).

Each row shows an icon, a human label and the time — and then prints the
record's remaining fields as `key=value` (everything except `ts`, `iso`,
`kind`, `category`, `host`, `seq`, which are already rendered as columns, up
to eight per row). The point of that rule is that nothing in a record is
hidden from you because the UI did not have a column for it.

## Maintenance (Data → Maintenance)

`GET /api/data/maintenance` backs four panels:

**Data retention.** A select over the allowed choices — Forever, 1 month,
3 months, **6 months (default)**, 1 year, 2 years. Saving calls
`POST /api/hub/system/retention`, which writes `data.retention_days` into
`ava.yaml` and returns `restart_required`; the panel shows a restart banner
rather than pretending the change is live. The panel's own note states the
scope honestly: it applies to performance rollups and hardware history, and
*chats and memories are never auto-deleted*.

**Database health.** `memory.db` size on disk, **reclaimable** bytes (SQLite
`page_size × freelist_count`), and when the last integrity check ran.

- **Check integrity** — `POST /api/data/maintenance/integrity` runs
  `PRAGMA integrity_check`; the result is stored so the panel can show
  "healthy" or "check failed" later, and the run is written to the audit
  ledger as a `data_maintenance` event.
- **Compact (VACUUM)** — `POST /api/data/maintenance/vacuum` returns bytes
  before and after, so the UI can tell you exactly how much it reclaimed
  (or that the file was already tight). Also audited.

**Export everything.** `GET /api/data/export` builds one `.zip` in memory:
`memory.json` (the whole store), `chats.json`, `audit.jsonl`, your `ava.yaml`,
and a `manifest.json` listing the contents. **Secrets and keys are never
included, and media is not bundled** — the manifest says so in as many words,
and points at copying `AVA_HOME` for a full backup. The export itself is
recorded as a `data_export` audit event.

**Backup.** The resolved `AVA_HOME` path and the total size across all stores.

## What long-term memory actually is

Memory is **lexical, not semantic**. `data/memory.db` (created `0600`) is
SQLite with an FTS5 virtual table tokenised `porter unicode61`; recall is a
`MATCH` query ranked by `bm25`, with pinned items sorted first. There is **no
embedding model, no vector database, no GPU contention with the brain, and
nothing extra to download on a fresh install.**

There are exactly **two item kinds**, and the writer rejects anything else:

- **`fact`** — a durable note about you, distilled from chat history by the
  learning cycle or typed in by hand under **Teach Ava**.
- **`doc`** — a chunk of an uploaded document's extracted text, indexed at
  upload time so a PDF you shared last month is still findable today.
  Chunking splits on paragraph boundaries at **~1200 characters** and stores
  at most **120 chunks per file**; re-uploading the same filename replaces
  that file's previous chunks rather than duplicating them.

**Recall is visible and accountable.** Hits are appended to the outgoing turn
inside a clearly labelled block that carries an explicit staleness caveat:

```
[Long-term memory — notes Ava saved earlier that look relevant. They may be
stale; prefer the user's current message on conflict.]
- The owner's dog is named Biscuit and is a corgi.
- [notes.pdf] Project kickoff is the first week of August.  (upload:notes.pdf)
```

Every recall that reaches a turn writes a `memory_recall` event to the audit
ledger with the item ids and sources it used — so *"why did she say that?"* is
always answerable, from **Data → Logs → Audit → Recalls** or
**Setup → History**. Manual edits (`memory_edit`) and distillation runs
(`memory_distill`) are logged the same way. The whole store leaves as plain
JSON in one click (`GET /api/hub/memory/export`).

The same **Memory** panel appears under both **Data → Memory** and
**Setup → Memory** — search, teach, edit, pin, forget, export.

> [**Memory & recall**](../MEMORY.md) covers the distillation cycle, the
> `memory.recall_k` / `memory.recall_max_chars` configuration keys, and the
> stated limitations of lexical retrieval. It is the reference; this page only
> describes the storage and transparency side.

## The audit ledger

One JSONL file, `logs/audit.jsonl`, is the durable substrate behind
**Setup → History**, the Recalls filter above, and every "this was recorded"
claim on this page. Its properties are structural, not conventional:

- **Append-only via `O_APPEND`**, one flat JSON line per event (atomic at this
  size on POSIX), `flock`'d against concurrent writers.
- **Mode `0600`** — it contains prompts and diff paths.
- **Best-effort** — a ledger failure can never break a chat turn; `record()`
  swallows its own exceptions rather than raising into the caller.
- **The agent's own edit tools cannot touch it.** The self-editing access
  policy hard-denies `logs/**` (alongside `data/**`, `secrets/**`,
  `models/**`, `.git/**`, `ava.yaml` and `connector_grants.yaml`), so no
  governed code change can rewrite the record of what it did. See
  [The agent](agent.md) for the rest of that policy.

Read it via `GET /api/hub/audit?limit=&kind=` or the Data → Logs tail.

## What retention does — and does not — reach

`data.retention_days` (default **183**, `0` = forever, choices 30 / 90 / 183 /
365 / 730, env override `AVA_DATA_RETENTION_DAYS`) is one knob, and it is
deliberately narrower than "everything Ava stores":

| Store | Governed by `data.retention_days`? |
|---|---|
| Performance rollups (hourly + daily) | **Yes** — both tiers pruned to it by the in-process rollup scheduler, which also prunes raw segments once absorbed |
| Hardware history | **Yes** — per tier, with the minute tier additionally hard-capped at **90 days** regardless of the setting, because minute resolution is what makes the file big |
| Generated media + uploads | **Eligibility only** — `prune_media()` applies the same cutoff, and `GET /api/data/maintenance` reports the reclaimable total, but the sweep runs when `POST /api/data/maintenance/prune-media` is called (see the honest note below) |
| **Audit ledger** | **No** — by design |
| **`memory.db`** | **No** — by design |
| **`chats.json`** | **No** — by design |

The three exclusions each have a reason the code states outright:

- **The audit ledger** is never truncated by the app, because *a ledger that
  prunes itself is not a ledger*. Rotation is the operator's business;
  `deploy/logrotate.conf` ships the config that bounds the file (~3.7 MB/year
  measured), which caps one file rather than reclaiming space.
- **Memory** and **chats** are yours to delete. There is no prune path in the
  memory store at all — removal is **Forget** in the Memory panel — and chats
  go one at a time from the Chats tab or the sidebar, audited. An assistant
  that quietly forgot things on a timer would be worse, not better.

So: retention governs telemetry and blobs. It does not govern the record of
what happened, or the things you asked Ava to remember.

## Honest notes

- **There is no "Reclaim space" button.** The Maintenance tab renders exactly
  four panels — retention, database health, export, backup. The media-prune
  route exists server-side and is covered by tests, but nothing in the UI
  calls it, and the Overview description of the generated-media store points
  at a control that is not there. Prune media by calling
  `POST /api/data/maintenance/prune-media` or by deleting from `media/out/`
  yourself.
- **Retention changes need a restart.** `perf_store` resolves the retention
  window at import; the panel tells you this instead of silently applying it
  on the next tick.
- **Item counts are skipped for very large files.** The inventory line-counts
  JSONL up to 64 MiB and reports bytes only beyond that, rather than stalling
  the whole page on one pathological log.
- **The allocation row only appears once `logs/alloc.jsonl` exists.** It is
  listed when it has content, because the point of inventorying it is that the
  log you are told to read before trusting the allocator should not be the one
  store whose size you cannot see. See
  [Running two models](../ALLOCATION.md).
- **`AVA_HOME` inside a git checkout means your data sits in the working
  tree.** That is fine for a single-user install, and `.gitignore` covers it,
  but if you fork Ava and work on the code, set `AVA_HOME` to a separate
  directory so a stray `git clean` cannot take your memory with it.

## Where this shows up elsewhere

- [**Vitals**](vitals.md) — what the performance and hardware-history stores
  are charted into.
- [**Operations**](operations.md) — the live view, and Setup → History's
  audit-backed flight recorder.
- [**The agent**](agent.md) — the access policy that keeps `logs/**` and
  `secrets/**` out of reach of governed code changes.
- [**Security model**](../../SECURITY.md) — trust boundaries, the full secret
  inventory, and sensitive-data handling.
