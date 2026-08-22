# Long-term memory - the reference

**This is the deep reference: the store, the recall path, the audit records, and
every configuration key.** For what memory does for you and how to use the
Memory panel, start at [Your data](capabilities/data.md).

Memory you cannot inspect is a liability, so Ava's is a plain SQLite file on your
disk with an audit trail for every time it influences a reply. Everything below
follows from that.

## What gets remembered

| Kind | Where it comes from |
|------|---------------------|
| **Facts** | Distilled from recent chats by a local-first LLM (see below), or added by hand in the **Memory** tab ("Teach Ava") - under **Setup → Agent → Memory**. |
| **Documents** | Text extracted from files you upload is chunked and indexed at upload time, so a PDF you shared last month is still findable today. |

Everything lives in `$AVA_HOME/data/memory.db` (created `0600`) - SQLite
with FTS5 full-text search. No embedding model, no vector database, no GPU
contention with the brain, nothing to download on a fresh install.

??? note "The store, exactly"

    The FTS5 virtual table is tokenised `porter unicode61`. Recall is a `MATCH`
    query ranked by `bm25`, with pinned items sorted first.

    There are exactly **two item kinds**, and the writer rejects anything else:

    - **`fact`** is a durable note about you, distilled from chat history by the
      distiller or typed in by hand under Teach Ava.
    - **`doc`** is a chunk of an uploaded document's extracted text, indexed at
      upload time. Chunking splits on paragraph boundaries at about **1200
      characters** and stores at most **120 chunks per file**; re-uploading the
      same filename replaces that file's previous chunks rather than
      duplicating them.

## How recall works

Each chat turn, the user message is matched against the store (FTS5, porter
stemming; pinned items rank first). Relevant hits are folded into the text
sent to the agent as a clearly labeled block:

```
[Long-term memory — notes Ava saved earlier that look relevant. They may be
stale; prefer the user's current message on conflict.]
- The owner's dog is named Biscuit and is a corgi.
- [notes.pdf] Project kickoff is the first week of August. (upload:notes.pdf)
```

Every recall that reaches a turn is written to the audit ledger
(`memory_recall` events - Data → History → Memory), so you can always answer
"why did she say that?" Manual edits (`memory_edit`) and distillation runs
(`memory_distill`) are logged the same way.

**Deletions are recorded by the store itself** (`memory_delete`), not by the route
that asked - so every path that removes memory leaves a trace, including the one
that is not user-initiated: re-uploading a document with the same filename
replaces its chunks, and that bulk delete is recorded with
`reason: "reindex: document re-uploaded"` rather than looking like an erasure.

Each record carries a **short content digest** of what was removed, never the text.
`tests/test_destructive_paths_audited.py` fails the build if a function that
destroys persisted data stops recording it.

??? note "Why a digest and not the text"

    The content is gone - that is what a delete means - so the ledger cannot
    quote it. A digest lets the record show *that a specific thing existed and
    was destroyed* without retaining it and without being reversible into it.

## How distillation works

An in-process scheduler (`ava_bridge/distill.py`, gated by `features.memory`,
cadence `memory.distill_interval_hours`, default 24h) reads chat messages it
hasn't seen before, asks the local brain for durable facts about *you* -
preferences, projects, setup, recurring people - and stores at most 8 per
cycle. One-off tasks and small talk are explicitly excluded. A cursor in the
store guarantees the same messages are never distilled twice.

**Local-only, by construction.** The prompt quotes your conversations verbatim,
so there is no cloud fallback: if your router cannot answer, the cycle stores
nothing and retries the same messages next time. This used to ride the Learning
feature's scheduler and could fall back to an Anthropic key; both went when
governed self-editing was removed.

## Controls, and where each one lives

Walked through for the reader in [Your data](capabilities/data.md); here as a
map from action to surface.

| Action | Surface | Note |
|---|---|---|
| Search / browse facts and document chunks | Setup → Agent → Memory | |
| Teach - add a fact directly | same panel | Written even with `features.memory: false` |
| Edit a fact; pin it to rank first | same panel | Logged as `memory_edit` |
| Forget an item | same panel | A whole upload's chunks go one at a time |
| Export the store as JSON | same panel | |
| Export everything (chats, ledger, settings, memory) | Data → Maintenance → Export archive | `GET /api/data/export` |
| See `memory.db` size and counts | Data → Overview | |
| Integrity check, or compact (VACUUM) | Data → Maintenance | Both land in the audit ledger |

## Configuration (`ava.yaml`)

```yaml
features:
  memory: true          # master switch (default on)
memory:
  recall_k: 4           # max items folded into one turn
  recall_max_chars: 2000  # recall block budget per turn
```

`features.memory: false` stops recall, document indexing and distillation -
nothing is folded into a turn and nothing new is remembered automatically.
Facts you add by hand in **Teach Ava** are still written. The existing store
stays on disk and browsable until you delete `$AVA_HOME/data/memory.db`.

## Limitations (honest edition)

- Retrieval is **lexical**, not semantic: it matches words (with stemming),
  so "my corgi" finds a memory that says "corgi", but a memory phrased only
  as "the dog" won't be found by "corgi". For personal-scale memory this is
  a good trade against shipping an embedding model; a semantic upgrade can
  slot in behind the same store later.
- Distillation quality depends on the local model; facts it gets wrong are
  yours to correct in **Setup → Agent → Memory** - that's the point of governed memory.
- Recall adds a small amount of text to each turn (bounded by
  `recall_max_chars`), which counts toward context like anything else.

---

**Where to next:** [Your data](capabilities/data.md) for the panel itself, or
[Privacy and proof](../SECURITY.md) for what memory is and is not allowed to
leave with.
