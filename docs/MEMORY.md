# Long-term memory — governed recall

Ava remembers things between conversations, and unlike every hosted
assistant, **you can read, correct, and delete everything she remembers**.
That governance is the design center: memory you can't inspect is a
liability, so Ava's is a plain SQLite file on your disk with an audit trail
for every time it influences a reply.

## What gets remembered

| Kind | Where it comes from |
|------|---------------------|
| **Facts** | Distilled by the learning cycle from recent chats (local-first LLM; see below), or added by hand in the **Memory** tab ("Teach Ava") — reachable from both **Setup → Memory** and **Data → Memory**. |
| **Documents** | Text extracted from files you upload is chunked and indexed at upload time, so a PDF you shared last month is still findable today. |

Everything lives in `$AVA_HOME/data/memory.db` (created `0600`) — SQLite
with FTS5 full-text search. No embedding model, no vector database, no GPU
contention with the brain, nothing to download on a fresh install.

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
(`memory_recall` events — Setup → History → Memory), so you can always answer
"why did she say that?" Manual edits (`memory_edit`) and distillation runs
(`memory_distill`) are logged the same way.

## How distillation works

The existing learning scheduler (`features.learning`) runs a
**memory distiller** alongside the code/chat analysis cycles: it reads chat
messages it hasn't seen before, asks the local brain (router; Anthropic key
as fallback) for durable facts about *you* — preferences, projects, setup,
recurring people — and stores at most 8 per cycle. One-off tasks and small
talk are explicitly excluded. A cursor in the store guarantees the same
messages are never distilled twice.

## Your controls (Setup → Memory, or Data → Memory — same panel)

- **Search / browse** facts and document chunks.
- **Teach** — add a fact directly.
- **Edit** any fact; **pin** anything to make it always rank first.
- **Forget** — delete an item (or a whole upload's chunks by deleting each).
- **Export** — the entire store as JSON in one click, or bundled with chats,
  the audit ledger, and your settings via **Data → Maintenance → Export
  archive** (`GET /api/data/export`).
- **Inspect the store itself** — **Data → Overview** shows `memory.db` size and
  counts; **Data → Maintenance** runs an integrity check or compacts it
  (VACUUM). Both actions land in the audit ledger.

## Configuration (`ava.yaml`)

```yaml
features:
  memory: true          # master switch (default on)
memory:
  recall_k: 4           # max items folded into one turn
  recall_max_chars: 2000  # recall block budget per turn
```

`features.memory: false` stops recall, document indexing and distillation —
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
  yours to correct in **Setup → Memory** — that's the point of governed memory.
- Recall adds a small amount of text to each turn (bounded by
  `recall_max_chars`), which counts toward context like anything else.
