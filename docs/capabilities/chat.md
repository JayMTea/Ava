# Chat, voice & creation

The chat view is where almost everything Ava does starts. Typing a message,
attaching a PDF, asking for a picture, holding the mic — all of it enters
through one surface, and **the server decides what happens next**. The browser
carries no routing knowledge, so what a turn becomes is auditable, testable,
and identical whether you asked from a laptop or a phone.

## One ingress, one gate

Every typed message posts to **`POST /api/chat-stream`**. There is no second
entry point: the same route handles a question, a follow-up, a file, and "draw
me a fox in the snow". The handler runs the server-side **intent gate**, which
picks the pipeline and returns either `{"turn_id": …}` for an agent turn or
`{"job": …}` for an image render. The client polls only to *paint* progress —
the bridge persists the outcome either way, so a browser that dies mid-render
costs you nothing.

Routing is tiered, and each tier exists because the one above it can fail:

| Tier | Who decides | What it does |
|------|-------------|--------------|
| **0** | The UI | Explicit affordances (an image button posts `/api/generate` directly). Declared intent needs no classifier. |
| **1** | The route handler | Deterministic short-circuits — attachments present, or empty text — go straight to the agent turn. |
| **2** | A small LLM call | One call classifies *and* extracts, under constrained decoding against a fixed JSON schema. |
| **3** | Regex | The demoted client-side heuristic, used on timeout, transport error, or malformed output. |

Tier 2 is the interesting one. The classifier is pinned to a JSON schema
(`{"route", "image_prompt"}`, `additionalProperties: false`) sent both as
OpenAI-style `response_format` *and* vLLM's `guided_json`, so an aligned model
physically cannot answer a classification request with refusal prose. Thinking
is off for the fast pass (2 s budget); the model escalates *itself* by emitting
the single `unsure` label, which triggers one thinking-on retry with a hard
token cap (8 s, 1024 tokens). Still unsure, or anything raises — Tier 3
answers. The worst case is "misrouted but answered and recorded", never
silence.

The labels are deliberately content-neutral, and the pipeline map is one dict:

| Label | Pipeline |
|-------|----------|
| `chat` | agent turn |
| `prompt_help` | agent turn, with a hint that says *edit this prompt, don't run it* |
| `image_new` | image job |
| `image_refine` | image job, prompt merged with the previous render's |

Owner content policy is a separate hook that runs **after** routing, never
inside the classifier.

Every decision writes a `route` event to the audit ledger with its label, tier,
whether it escalated, latency, and mode — visible in **Setup → History**. And
because routing behaviour is a function of the prompt, the schema, the labels,
the mode, the endpoint and the context template, the gate is **fingerprinted**:
a 16-character hash of all of it. `ava doctor` prints the current fingerprint
alongside your eval-set accuracy and nudges a re-run when it changes.

```yaml
routing:
  mode: llm             # or "regex" to pin the deterministic fallback
  timeout_s: 2.0        # fast pass
  think_timeout_s: 8.0  # escalated pass
  think_budget: 1024    # hard token cap on the escalated pass
```

## Live chain of thought

The agent CLI is non-streaming — one JSON blob at the end — so Ava does not
stream reasoning tokens. She does something better-founded: **the agent
persists every step to its session `jsonl` as it works**, and the bridge runs
the blocking turn in a worker thread while concurrently tailing that file
(~1 Hz). Assistant `thinking`, intermediate `text`, and `toolCall` blocks
become ordered CoT steps on the turn, which the UI paints live under a
collapsible "Thought for *N*s · *N* steps" header.

That trajectory is **durable**. The completed step list is saved with the chat
message, so reopening a conversation replays the reasoning above the reply
exactly as it appeared live — collapsed, marked done. Nothing is reconstructed
or re-inferred.

The tool-less [Direct floor](../AGENT_RUNTIME.md) has no sandbox and therefore
no session file, so it has no live chain of thought. That is the honest
trade-off of running without a runtime, not a bug.

## Ghost mode

Ghost mode is a conversation that leaves no trace. The chat id is unregistered,
so nothing is ever written to `data/chats.db` — host-side persistence is already
a no-op rather than a delete-afterwards. When you leave ghost mode (toggle off,
start a new chat, open an old one, or close the tab), the UI calls
`POST /api/ghost/discard`, which **deletes the agent's session transcript** so
the runtime-side memory of the conversation goes too.

## Files you attach

The agent CLI is text-only, so uploads are ingested host-side and folded into
the message:

| Type | How text is extracted |
|------|----------------------|
| PDF | `pdftotext -layout` |
| Office / ODF (`.docx`, `.xlsx`, `.pptx`, `.odt`, …) | headless LibreOffice (`soffice --convert-to txt`) |
| Plain text, CSV, JSON, Markdown, logs | read directly |
| Images | `tesseract` OCR, when installed |

**There is no vision model.** An attached image is stored and shown in the
chat, but only OCR'd text reaches Ava — and if OCR finds nothing (or
`tesseract` isn't installed), the message says so plainly: *"The user attached
an image … No text could be extracted from it."* Ava is told the truth rather
than being left to guess at a picture she cannot see.

Extracted text is appended to your message under a labelled `--- Attached … ---`
block, and is also chunked into the memory store at upload time, so a document
you shared last month stays findable — see
[Memory & recall](../MEMORY.md). Uploads are capped at 25 MB
(`AVA_MAX_UPLOAD_MB`) with 24,000 characters folded into a turn
(`AVA_MAX_DOC_CHARS`), and the allowed extension list is a fixed set so the
upload directory cannot be used to stash executables or keys.

## Images: generate and upscale

Renders take 15–60 seconds, so they run as **async jobs**, not blocking
requests. `POST /api/generate` and `POST /api/upscale` return a job id
immediately; `GET /api/job/{id}` reports progress and
`POST /api/job/{id}/cancel` stops it. The chat bubble shows the live
percentage, the current stage (`queued` → `rendering` → `upscaling`), a queue
hint, elapsed seconds, a **Cancel** button, and a collapsible **Prompt details**
disclosure.

The **bridge owns each job's outcome**, not the browser. A job bound to a chat
gets its terminal state persisted as a chat message — the image on success, a
coded error on failure — and every real render lands in the audit ledger. If a
render fails because GPU workloads is switched off or the GPU service is down, the
job is born errored with the code `image_off` / `image_down`, and the chat
renders a guided fix-it link instead of a spinner that never ends.

Generated PNGs are full resolution (4K files run past 16 MB) but the chat shows
them around 500 px, so **`ensure_thumbnail` lazily writes a small WebP** (~75 KB)
on first request for `/thumb/{name}` and caches it on disk. It is generated on
demand, so it covers images that predate the feature. Without Pillow installed
the route serves the full image and says why, once, on stderr.

Image bubbles offer **Upscale to 4K** (the refiner, itself an async job), a
tap-to-open lightbox with pinch/wheel/double-tap zoom, and an automatic privacy
blur after 30 seconds so a generated image isn't left on screen indefinitely.

GPU workloads is one switch — `features.image` — which governs the render
path and how the dashboard paints the GPU service, so "off" never gets reported as
"down".

## Side-panel artifacts

When a turn uses a tool with a rich visual companion, Ava opens a resizable
split panel beside the conversation. **Weather is the one implemented
renderer** today: hero conditions, feels-like / humidity / wind, a week chart of
highs, lows and precipitation, and daily rows.

What makes it trustworthy is *how* it is built. The panel is **not** parsed out
of the model's prose. The bridge reads the `get_weather` tool-call arguments the
agent actually used — from the session jsonl lines appended since this turn
started, so a stale earlier call can't leak in — then re-fetches structured data
itself from the same public source the tool uses (Open-Meteo, no API key). If
anything fails the artifact is simply omitted; it never blocks or breaks a
normal reply.

The panel has **Refresh** (re-runs `GET /api/artifact/weather`) and **Close**,
and the divider between chat and panel is drag-resizable by mouse or touch.

## Voice

Voice is a full round trip, and every hop degrades rather than fails:

1. **Capture** — push-to-talk in the composer via `MediaRecorder`; releasing
   the button posts the clip to `POST /api/talk`.
2. **Decode** — `ffmpeg` converts whatever container the browser produced
   (webm/opus, mp4/aac) to 16 kHz mono `s16le` PCM. Clips under ~0.5 s come
   back with "hold the button and speak a full sentence" rather than an error.
3. **Speaker gate** — an ECAPA embedding of the clip is compared to your
   enrolled voiceprint by cosine similarity. Below the threshold (default
   `0.40`, `voice.threshold` in `ava.yaml`) Ava answers *"Sorry, I only respond
   to the enrolled voice"* and the turn never reaches the agent.
4. **Transcribe** — with `AVA_STT=gpu`, the GPU Whisper sidecar on `:8129`;
   if that service is unreachable or errors, it falls back to local CPU Whisper.
   A stopped sidecar slows STT; it never breaks voice.
5. **Answer** — the transcript goes through the same agent path as a typed
   message, with the same recall and tools.
6. **Speak** — with `AVA_TTS=kokoro`, the Kokoro service (natural voice, GPU)
   returns the WAV; on any failure it transparently falls back to Piper, so a
   stopped `kokoro-tts` service degrades voice *quality* but never voice.

Voice is an optional capability (`features.voice`, off by default). Turning it
off is honoured at the route: `/api/talk` returns a coded `voice_off` response
rather than quietly recording anyway.

### Enrolling and testing your voiceprint

**Setup → Voice** is the whole flow, and it is candid about the security
property at stake — with voice on and no voiceprint enrolled, the panel says
plainly that **the gate is open** and anyone can talk to Ava.

- **Enroll your voice** — record a few clips of natural speech in the browser,
  then **Build voiceprint**. Ava windows the speech (4 s windows, 2 s hop),
  drops near-silent windows, embeds each with ECAPA, rejects outliers more than
  one standard deviation below the mean similarity, and saves the averaged
  voiceprint. Nothing is uploaded anywhere; it stays on this machine.
- **Read the quality stats** — seconds captured, windows used, windows dropped,
  and min/mean/max consistency, plus a **suggested threshold** derived from the
  worst surviving window. **Apply threshold** saves it.
- **Discard clips** if you'd rather start over.
- **Test the gate** — record one more clip and see its similarity scored
  against the threshold, with a plain-English verdict.

## What the chat surface actually gives you

| Affordance | What it does |
|------------|--------------|
| **Copy** | Copies Ava's reply text. |
| **Retry** | Re-asks the same thing, attachments included. |
| **Replay** | Replays the spoken WAV for a voice turn. |
| **Tools used (*n*)** | Expands the tool list for that turn; each chip carries the owning app's identity colour. |
| **Chain of thought** | Collapse or expand the reasoning trace, live or replayed. |
| **Context meter** | Tokens used against the model's usable window (`inference.ctx_max`, default 65,536) — amber from 70 %, red from 90 %. |
| **Attachments** | Multi-file picker with per-file chips; images marked *"text read"* when OCR found something. |
| **Push-to-talk mic** | Records and sends a voice turn. |
| **Jump to latest** | Appears when you scroll back up. |
| **New chat / delete chat** | From the sidebar; deletions are recorded in the audit ledger. |
| **Ghost mode** | The ephemeral conversation described above. |

Chats title themselves from your first message (truncated to 48 characters);
the sidebar's search box filters the recents list by title.

## Two things the UI tells you, and so do we

**The header model picker sets the *fallback* model, not the one that answers.**
With the agent runtime active — the default — chat turns think with the
**sandbox model**, configured by `nemoclaw onboard`, and bypass the inference
router entirely. The picker steers the router's backends, which is the path used
if the agent is stopped. The control's own tooltip says exactly this
("*Ava answers with … (agent sandbox). This picker only sets the fallback model
used if the agent is stopped.*"), and the documentation matches it. For picking
what actually thinks, see [Choosing a model](../CHOOSE_A_MODEL.md) and
[the agent runtime](../AGENT_RUNTIME.md).

**The composer's "Code mode" toggle is not wired in this UI.** Flipping it and
sending a message returns a system line pointing at the classic UI at `/legacy`.
Self-editing is real, but it is driven from **Operations → Control Center**, not
from the chat composer — see
[the agent: tools, skills & self-improvement](agent.md).

## Where to go next

- [Memory & recall](../MEMORY.md) — what gets folded into a turn, why, and how
  to read, correct or delete it.
- [On your phone (PWA)](../MOBILE.md) — installing Ava as a home-screen app.
  Voice capture works there; there are no push notifications.
- [Operations](operations.md) — the live view of turns, render jobs and the
  approvals that pause a tool call.
- [Data, memory & privacy](data.md) — where chats, uploads and generated media
  actually live on disk, and how to export or delete them.
