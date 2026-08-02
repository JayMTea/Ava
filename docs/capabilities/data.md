# Data, memory & privacy

**Everything Ava knows about you sits in one folder on your own machine, and
you can read it, correct it or delete it at any time.**

That is the promise. The rest of this page is the evidence for it: every store
Ava keeps, named and sized and pointed at its exact path, plus the controls
that read, export and erase each one. A hosted assistant can tell you it
respects your privacy. It cannot show you the files.

[![What stays on your machine and what leaves only if you switch it on. Staying: your chats and history, what Ava remembers about you, your files and images, your voiceprint, the model weights, your connected apps' data, your secrets and API keys. Leaving only when switched on: a web search, your prompt to a cloud model you picked, one learning cycle, a model download, and reaching Ava from your phone](../assets/egress.svg)](../assets/egress.svg)

Each box on the right carries the switch that opens it. Every one of them is
off, unconfigured, or not yet asked for on a fresh install: `features.web_search`
and `features.learning_cloud_fallback` both default to `false`
(`ava_bridge/features.py`), no cloud model is configured as the brain, and the
bridge binds to loopback only.

## You hold the eraser

Three things you can do to anything Ava keeps. The **Data** page holds every
store, the audit ledger and the maintenance tools; memory's own browser lives
one click away under **Setup → Agent → Memory**, which the memory store row
links to. Every one of these is written to the audit ledger, so the change
itself leaves a trace.

| | What you can do | Where | What it writes |
|---|---|---|---|
| **Read** | See every store, its size, its path and when it was last written. Search what Ava remembers. Read any conversation. Tail the logs. | Data → Overview / Memory / Chats / Logs | nothing |
| **Correct** | Edit a remembered fact, pin it so it is recalled first, or type in a new one under **Teach Ava**. Ava's memory is a list you can hand-edit. | Setup → Agent → Memory (the Data page links to it) | `memory_edit` |
| **Delete** | Forget one fact. Delete one conversation. Empty a whole store in one action, after a confirm that names how much is in it. | Setup → Agent → Memory, Data → Chats, or **Empty** on any Overview row | `memory_edit`, `chat_delete`, `data_delete` |

Deleting a store is `DELETE /api/data/stores/{id}`, and the receipt tells you
what went: rows for `memory` and `chats`, files and bytes for everything else.

??? note "Three stores refuse to be emptied, and each says why"

    A refusal that cannot say why is indistinguishable from an oversight, so
    each of these returns its own reason rather than a blanket "dangerous"
    (`ava_bridge/data_api.py`, `_REFUSED`):

    - **The audit ledger.** It is the record of every other deletion, including
      the ones this page performs. A ledger that prunes itself is not a ledger.
      Rotation is the operator's business; `deploy/logrotate.conf` ships the
      config, about 3.7 MB/year measured.
    - **Secrets and keys.** Removing them locks you out and breaks the sandbox:
      they hold the login password, the session-signing key and the agent's
      internal token. Rotate them individually instead. Change the password in
      Setup → System, or clear one connector credential in Setup → Connectors.
    - **Models.** The voiceprint has its own destruction path (Setup → Agent → Voice),
      which also resets the derived gate threshold and evicts the copy cached
      in the running process. A plain delete here would leave the gate scoring
      against a print that no longer exists, and would also remove the 80 MB
      public ECAPA weights, which contain nothing about you. See
      [Biometrics](../BIOMETRICS.md).

## One folder

Everything Ava persists resolves under **`AVA_HOME`**. On a default bare-metal
install `AVA_HOME` is the code root itself (`ava_bridge/settings.py`), so a
fresh clone ends up like this:

```
ava/                    <- the checkout, and AVA_HOME
├── ava.yaml            <- your settings
├── data/               <- memory.db, chats.db, session key, tokens, password
├── logs/               <- audit ledger, performance, hardware history, devices
├── media/              <- gen/ (generated images), uploads/ (files you shared)
└── secrets/            <- backend API keys, router token
```

Copy that folder and you have copied everything: memories, chats, config and
keys. Restore by pointing `AVA_HOME` at the copy. **Data → Maintenance →
Backup** prints the resolved path and the total bytes so you know exactly what
you are copying.

!!! note "Out of the box, your data lives inside the checkout"

    That is a deliberate default, and `.gitignore` covers it. But if you fork
    Ava and work on the code, set `AVA_HOME` to a separate directory so a stray
    `git clean` cannot take your memory with it.

??? note "Moving the root, or one directory out of it"

    Point `AVA_HOME` somewhere else (an env var, or the compose volume, which
    mounts `./ava-data`) and every path above moves with it. Individual
    directories can also be redirected on their own with `paths.data`,
    `paths.logs`, `paths.media`, `paths.uploads` and `paths.secrets`.

## The inventory (Data → Overview)

![Ava's Data overview: one row per store, each with a format badge, what it holds, its AVA_HOME-relative path in code, bytes on disk, an item count and when it was last written](../assets/data-overview.png)

The secrets row is the one to look at, because it is where the promise is
easiest to break. Ava lists the *names* it holds so you can audit them, and
never the values:

![The Secrets row: badges reading LOCKED, "never leaves this machine" and "protected"; the path secrets.json; then six named entries with descriptions and no values - login_password, session_key, anthropic_api_key, fitness_token, ledger_token and home_assistant_token; footer reads 6 items held, written 1h ago](../assets/data-secrets.png)

`GET /api/data/stores` walks the real filesystem and returns one entry per
store. Each renders as a row with a format badge (`SQLITE` / `JSON` / `JSONL` /
`FILES` / `LOCKED`), a description of what it holds, the `AVA_HOME`-relative
path as literal code, bytes on disk, an item count, and when it was last
written. Stores with automatic retention carry an `auto-managed` badge; the
secrets row carries `never leaves this machine`.

| Store | Format | What it holds |
|---|---|---|
| **Memory** | `data/memory.db`, SQLite | Distilled facts and indexed document chunks; row shows facts / doc chunks / pinned counts |
| **Chats** | `data/chats.db`, SQLite | Every conversation; row shows chat count and total messages |
| **Audit ledger** | `logs/audit.jsonl` | The flight recorder: turns, recalls, memory edits, grants, self-edits |
| **Performance** | `logs/performance.jsonl` plus rotated segments and `logs/rollups/` | Per-generation throughput, latency and energy, plus the hourly and daily rollups behind Vitals |
| **Allocation decisions** | `logs/alloc.jsonl` | What was asked for, what the pool held, what was released (row appears once the file exists) |
| **Hardware history** | `logs/hw_history/` | Minute- and hour-resolution GPU, memory and CPU samples |
| **Device events** | `logs/devices/` | One rotated JSONL stream per connector; counted in *streams*, not lines |
| **Generated media** | `media/out/` | Images and video Ava rendered |
| **Uploads** | `media/uploads/` | Files you shared in chat; the originals stay here after their text is indexed |
| **Secrets & keys** | `data/` and `secrets/` | Session signing key, internal tokens, login password, backend API keys |

??? note "Why this is an inventory you can trust rather than a dashboard you take on faith"

    **The Data API never returns file contents.** The module says so at the top:
    browsing and editing go through the existing governed surfaces
    (`/api/hub/memory*`, `/api/chats*`), while `/api/data/*` returns sizes,
    counts and timestamps. The only reads of actual records are the whitelisted
    log tails.

    **Secrets are listed by name and purpose only.** The secrets row expands
    into `name - purpose` pairs so you know exactly what is held, and the values
    are never opened, displayed, or exported. The purposes are the honest ones
    the code actually ships:

    ```
    .secret          Session signing key
    .internal_token  Internal API token
    auth_password    Login password (stored 0600, not hashed)
    router_token     Internal router token
    ```

    That last label is the tone the whole page is written in. The source comment
    next to it explains why it reads that way: `auth.set_password` writes the
    value verbatim at mode `0600`, and saying "hashed" in a security inventory
    overstated the protection. See [Security model](../../SECURITY.md) for the
    full secret inventory and what each one gates.

??? note "Chats, logs and maintenance, endpoint by endpoint"

    **Chats (Data → Chats).** One row per conversation (title, message count,
    JSON weight, last update), newest first, from `GET /api/data/chats`.

    - **JSON** is `GET /api/data/chats/{id}/export`, the conversation verbatim.
    - **MD** is `?format=md`, a readable Markdown transcript with timestamps,
      generated-image links and attachment names.
    - **Delete** is `DELETE /api/chats/{id}`, behind a confirm that tells you
      what it is about to do. It is permanent, and it is recorded in the audit
      ledger as a `chat_delete` event.

    **Logs (Data → Logs).** Tails of the append-only stores, newest first,
    straight from the files under `logs/`:
    `GET /api/data/logs/{name}/tail?n=&kind=`. The name is a fixed whitelist
    (`audit`, `performance`, `devices`), so there is no arbitrary-path read
    here. The UI pulls the last 100 records and refreshes every 15 s; the route
    caps `n` at 500.

    On the Audit source, sub-filters narrow by event kind: All, Turns, Recalls,
    Memory edits, Grants, Chat deletes (`turn`, `memory_recall`, `memory_edit`,
    `grant`, `chat_delete`). Each row shows an icon, a human label and the time,
    then prints the record's remaining fields as `key=value` (everything except
    `ts`, `iso`, `kind`, `category`, `host`, `seq`, which are already columns, up
    to eight per row). Nothing in a record is hidden from you because the UI did
    not have a column for it.

    **Maintenance (Data → Maintenance).** `GET /api/data/maintenance` backs four
    panels:

    - **Data retention.** A select over Forever, 1 month, 3 months, **6 months
      (default)**, 1 year, 2 years. Saving calls
      `POST /api/hub/system/retention`, which writes `data.retention_days` into
      `ava.yaml` and returns `restart_required`; the panel shows a restart
      banner rather than pretending the change is live.
    - **Database health.** `memory.db` size on disk, reclaimable bytes (SQLite
      `page_size × freelist_count`), and when the last integrity check ran.
      **Check integrity** (`POST /api/data/maintenance/integrity`) runs
      `PRAGMA integrity_check`; **Compact (VACUUM)**
      (`POST /api/data/maintenance/vacuum`) returns bytes before and after, so
      the UI can say exactly how much it reclaimed. Both are written to the
      ledger as `data_maintenance` events.
    - **Export everything.** `GET /api/data/export` builds one `.zip` in memory:
      `memory.json` (the whole store), `chats.json`, `audit.jsonl`, your
      `ava.yaml`, and a `manifest.json` listing the contents. **Secrets and keys
      are never included, and media is not bundled.** The manifest says so in as
      many words and points at copying `AVA_HOME` for a full backup. The export
      is recorded as a `data_export` audit event.
    - **Backup.** The resolved `AVA_HOME` path and the total size across all
      stores.

## What Ava remembers about you

Memory is a **list of short written notes**, not a model that has absorbed you.
Each one is a sentence you can read, edit or delete, and they come from two
places: Ava distils them from your conversations on a schedule, or you type
them in yourself under **Teach Ava**. Uploaded documents are indexed the same
way, so a PDF you shared last month is still findable today.

When a note is used, it is used visibly. Hits are appended to the outgoing turn
inside a clearly labelled block that carries an explicit staleness caveat:

```
[Long-term memory - notes Ava saved earlier that look relevant. They may be
stale; prefer the user's current message on conflict.]
- The owner's dog is named Biscuit and is a corgi.
- [notes.pdf] Project kickoff is the first week of August.  (upload:notes.pdf)
```

Every recall that reaches a turn writes a `memory_recall` event to the audit
ledger with the item ids and sources it used, so *"why did she say that?"* is
always answerable, from **Data → Logs → Audit → Recalls** or **Setup →
History**. Manual edits (`memory_edit`) and distillation runs
(`memory_distill`) are logged the same way. The whole store leaves as plain
JSON in one click (`GET /api/hub/memory/export`).

!!! note "The mechanism lives on one page"

    [Memory & recall](../MEMORY.md) is the reference: how distillation decides
    what is durable, how recall ranks and truncates, the `memory.recall_k` and
    `memory.recall_max_chars` keys, and the stated limitations of lexical
    retrieval. This page covers storage, visibility and your controls.

??? note "Where it is stored, in one paragraph"

    `data/memory.db` (created `0600`) is SQLite with FTS5 full-text search.
    Recall is **lexical, not semantic**: it matches words, not meanings. There
    is **no embedding model, no vector database, no GPU contention with the
    brain, and nothing extra to download on a fresh install.** The table
    layout, the ranking, the two item kinds and the document chunk sizes are in
    [Memory and recall](../MEMORY.md).

## The audit ledger

One JSONL file, `logs/audit.jsonl`, is the durable substrate behind **Setup →
History**, the Recalls filter above, and every "this was recorded" claim on this
page. Read it via `GET /api/hub/audit?limit=&kind=` or the Data → Logs tail.

??? note "Why the ledger can be trusted about itself"

    Its properties are structural, not conventional:

    - **Append-only via `O_APPEND`**, one flat JSON line per event (atomic at
      this size on POSIX), `flock`'d against concurrent writers.
    - **Mode `0600`**, because it contains prompts and diff paths.
    - **Best-effort.** A ledger failure can never break a chat turn; `record()`
      swallows its own exceptions rather than raising into the caller.
    - **The agent's own edit tools cannot touch it.** The self-editing access
      policy hard-denies `logs/**`, alongside `data/**`, `secrets/**`,
      `models/**`, `.git/**`, `ava.yaml` and `connector_grants.yaml`, so no
      governed code change can rewrite the record of what it did. See
      [The agent](agent.md) for the rest of that policy.

## What retention does, and does not, reach

Retention governs telemetry and blobs. It does not govern the record of what
happened, or the things you asked Ava to remember. **Chats and memories are
never auto-deleted**; they go when you delete them.

??? note "The exact scope of `data.retention_days`"

    `data.retention_days` (default **183**, `0` = forever, choices 30 / 90 / 183
    / 365 / 730, env override `AVA_DATA_RETENTION_DAYS`) is one knob, and it is
    deliberately narrower than "everything Ava stores":

    | Store | Governed by `data.retention_days`? |
    |---|---|
    | Performance rollups (hourly and daily) | **Yes.** Both tiers are pruned to it by the in-process rollup scheduler, which also prunes raw segments once absorbed |
    | Hardware history | **Yes**, per tier, with the minute tier additionally hard-capped at **90 days** regardless of the setting, because minute resolution is what makes the file big |
    | Generated media and uploads | **Eligibility only.** `prune_media()` applies the same cutoff and `GET /api/data/maintenance` reports the reclaimable total, but the sweep runs when `POST /api/data/maintenance/prune-media` is called (see the honest notes below) |
    | **Audit ledger** | **No**, by design |
    | **`memory.db`** | **No**, by design |
    | **`chats.db`** | **No**, by design |

    The three exclusions each have a reason the code states outright. The audit
    ledger is never truncated by the app, because a ledger that prunes itself is
    not a ledger. Memory and chats are yours to delete: there is no prune path
    in the memory store at all, removal is **Forget** in the Memory panel, and
    chats go one at a time from the Chats tab or the sidebar, audited. An
    assistant that quietly forgot things on a timer would be worse, not better.

## Honest notes

- **There is no "Reclaim space" button.** The Maintenance tab renders exactly
  four panels: retention, database health, export, backup. The media-prune
  route exists server-side and is covered by tests, but nothing in the UI calls
  it, and the Overview description of the generated-media store points at a
  control that is not there. Prune media by calling
  `POST /api/data/maintenance/prune-media`, by emptying the Generated media
  store from Overview, or by deleting from `media/out/` yourself.
- **Retention changes need a restart.** `perf_store` resolves the retention
  window at import; the panel tells you this instead of silently applying it on
  the next tick.
- **Item counts are skipped for very large files.** The inventory line-counts
  JSONL up to 64 MiB and reports bytes only beyond that, rather than stalling
  the whole page on one pathological log.
- **The allocation row only appears once `logs/alloc.jsonl` exists.** It is
  listed when it has content, because the log you are told to read before
  trusting the allocator should not be the one store whose size you cannot see.
  See [Running two models](../ALLOCATION.md).

## Where this shows up elsewhere

- [**Memory & recall**](../MEMORY.md) is the reference for distillation, recall
  and its limits.
- [**Vitals**](vitals.md) is what the performance and hardware-history stores
  are charted into.
- [**Operations**](operations.md) is the live view, and Data → History's
  audit-backed flight recorder.
- [**The agent**](agent.md) holds the access policy that keeps `logs/**` and
  `secrets/**` out of reach of governed code changes.
- [**Security model**](../../SECURITY.md) covers trust boundaries, the full
  secret inventory, and sensitive-data handling.
