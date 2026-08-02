# Chat, voice & creation

The chat view is where almost everything Ava does starts. Typing a message,
attaching a PDF, asking for a picture, tapping the mic - all of it enters
through one surface, and **the server decides what happens next**. The browser
carries no routing knowledge, so what a turn becomes is auditable, testable,
and identical whether you asked from a laptop or a phone.

![Ava's chat view: a plain-language question about Saturday's calendar and weather, Ava's answer, and under each reply the model that answered it and a "Tools used" chip listing the tools she actually called](../assets/chat.png)

## What you can actually do here

| Control | What it does |
|------------|--------------|
| **Copy** | Copies Ava's reply text. |
| **Retry** | Re-asks the same thing, attachments included. |
| **Replay** | Replays the spoken WAV for a voice turn. |
| **Tools used (*n*)** | Expands the tool list for that turn; each chip carries the owning app's identity colour. |
| **Chain of thought** | Collapse or expand the reasoning trace, live or replayed. |
| **Context meter** | Tokens used against the model's usable window (`inference.ctx_max`, default 65,536) - amber from 70 %, red from 90 %. |
| **Attachments** | Multi-file picker with per-file chips; images marked *"text read"* when OCR found something. |
| **Mic** | Tap to start recording, tap again to send it as a voice turn. |
| **Jump to latest** | Appears when you scroll back up. |
| **New chat / delete chat** | From the sidebar; deletions are recorded in the audit ledger. |
| **Ghost mode** | A conversation that leaves no trace. See below. |

**Tools used** is the one worth opening. It is how you check Ava's work: the
answer above it came from these calls, on this machine, and nothing else.

![The "Tools used (2)" control expanded under a reply, listing the two tools that turn actually called: calendar.read and weather.forecast](../assets/chat-tools-used.png)

Chats title themselves from your first message (truncated to 48 characters);
the sidebar's search box filters the recents list by title.

## How a message gets routed

Everything you send - a question, a follow-up, a file, "draw me a fox in the
snow" - posts to the single endpoint **`POST /api/chat-stream`**. A
server-side gate reads it and picks the pipeline: an agent turn, or an image
render. If the gate is unsure or breaks, the worst case is a message that was
answered by the wrong pipeline and recorded as such, never silence.

??? info "The four routing tiers, the fingerprint, and the `routing:` config"

    Routing is tiered, and each tier exists because the one above it can fail:

    | Tier | Who decides | What it does |
    |------|-------------|--------------|
    | **0** | The UI | Explicit affordances (an image button posts `/api/generate` directly). Declared intent needs no classifier. |
    | **1** | The route handler | Deterministic short-circuits - attachments present, or empty text - go straight to the agent turn. |
    | **2** | A small LLM call | One call classifies *and* extracts, under constrained decoding against a fixed JSON schema. |
    | **3** | Regex | The demoted client-side heuristic, used on timeout, transport error, or malformed output. |

    Tier 2 is the interesting one. The classifier is pinned to a JSON schema
    (`{"route", "image_prompt"}`, `additionalProperties: false`) sent both as
    OpenAI-style `response_format` *and* vLLM's `guided_json`, so an aligned
    model physically cannot answer a classification request with refusal
    prose. Thinking is off for the fast pass (2 s budget); the model escalates
    *itself* by emitting the single `unsure` label, which triggers one
    thinking-on retry with a hard token cap (8 s, 1024 tokens). Still unsure,
    or anything raises - Tier 3 answers.

    The labels are deliberately content-neutral, and the pipeline map is one
    dict:

    | Label | Pipeline |
    |-------|----------|
    | `chat` | agent turn |
    | `prompt_help` | agent turn, with a hint that says *edit this prompt, don't run it* |
    | `image_new` | image job |
    | `image_refine` | image job, prompt merged with the previous render's |

    Owner content policy is a separate hook that runs **after** routing, never
    inside the classifier.

    Every decision writes a `route` event to the audit ledger with its label,
    tier, whether it escalated, latency, and mode - visible in
    **Data → History**. And because routing behaviour is a function of the
    prompt, the schema, the labels, the mode, the endpoint and the context
    template, the gate is **fingerprinted**: a 16-character hash of all of it.
    `ava doctor` prints the current fingerprint alongside your eval-set
    accuracy and nudges a re-run when it changes.

    ```yaml
    routing:
      mode: llm             # or "regex" to pin the deterministic fallback
      timeout_s: 2.0        # fast pass
      think_timeout_s: 8.0  # escalated pass
      think_budget: 1024    # hard token cap on the escalated pass
    ```

The handler returns either `{"turn_id": …}` for an agent turn or
`{"job": …}` for an image render. The client polls only to *paint* progress;
the bridge persists the outcome either way, so a browser that dies mid-render
costs you nothing.

## Live chain of thought

While Ava works, you watch her work. Each step appears under a collapsible
**"Thought for *N*s · *N* steps"** header, and it is a record of what actually
happened rather than a stream of reasoning tokens: the agent writes every step
to its session file as it goes, and the bridge tails that file about once a
second.

That trajectory is **durable**. The completed step list is saved with the chat
message, so reopening a conversation replays the reasoning above the reply
exactly as it appeared live - collapsed, marked done. Nothing is reconstructed
or re-inferred.

!!! note "The tool-less path has no chain of thought"

    The [Direct floor](../AGENT_RUNTIME.md) runs without a sandbox and
    therefore without a session file, so it has no live chain of thought. That
    is the honest trade-off of running without a runtime, not a bug.

## Ghost mode

Ghost mode is a conversation that leaves no trace. The chat id is
unregistered, so nothing is ever written to `data/chats.db` - host-side
persistence is a no-op rather than a delete-afterwards. When you leave ghost
mode (toggle off, start a new chat, open an old one, or close the tab), the UI
calls `POST /api/ghost/discard`, which **deletes the agent's session
transcript** so the runtime-side memory of the conversation goes too.

## Files you attach

Attach a document and Ava reads its text. Uploads are capped at 25 MB
(`AVA_MAX_UPLOAD_MB`) with 24,000 characters folded into a turn
(`AVA_MAX_DOC_CHARS`), and the allowed extension list is a fixed set so the
upload directory cannot be used to stash executables or keys.

| Type | How text is extracted |
|------|----------------------|
| PDF | `pdftotext -layout` |
| Office / ODF (`.docx`, `.xlsx`, `.pptx`, `.odt`, …) | headless LibreOffice (`soffice --convert-to txt`) |
| Plain text, CSV, JSON, Markdown, logs | read directly |
| Images | `tesseract` OCR, when installed |

**There is no vision model.** An attached image is stored and shown in the
chat, but only OCR'd text reaches Ava. If OCR finds nothing (or `tesseract`
isn't installed), the message says so plainly: *"The user attached an image …
No text could be extracted from it."* Ava is told the truth rather than being
left to guess at a picture she cannot see.

Extracted text is also chunked into the memory store at upload time, so a
document you shared last month stays findable - see
[Memory & recall](../MEMORY.md).

## Images: generate and upscale

Renders take 15-60 seconds, so they run as background jobs. The chat bubble
shows the live percentage, the current stage (`queued` → `rendering` →
`upscaling`), a queue hint, elapsed seconds, a **Cancel** button, and a
collapsible **Prompt details** disclosure.

Close the tab and the render still finishes. The **bridge owns each job's
outcome**, not the browser: a job bound to a chat gets its terminal state
persisted as a chat message - the image on success, a coded error on failure -
and every real render lands in the audit ledger. If a render fails because
GPU workloads is switched off or the GPU service is down, the job is born errored
with the code `image_off` / `image_down`, and the chat renders a guided fix-it
link instead of a spinner that never ends.

Image bubbles offer **Upscale to 4K** (the refiner, itself a background job),
a tap-to-open lightbox with pinch/wheel/double-tap zoom, and an automatic
privacy blur after 30 seconds so a generated image isn't left on screen
indefinitely.

GPU workloads is one switch - `features.image` - which governs the render
path and how the dashboard paints the GPU service, so "off" never gets reported as
"down".

??? note "Job endpoints, and why the thumbnail is a WebP"

    `POST /api/generate` and `POST /api/upscale` return a job id immediately;
    `GET /api/job/{id}` reports progress and `POST /api/job/{id}/cancel` stops
    it.

    Generated PNGs are full resolution (4K files run past 16 MB) but the chat
    shows them around 500 px, so **`ensure_thumbnail` lazily writes a small
    WebP** (~75 KB) on first request for `/thumb/{name}` and caches it on disk.
    It is generated on demand, so it covers images that predate the feature.
    Without Pillow installed the route serves the full image and says why,
    once, on stderr.

## Side-panel artifacts

When a turn uses a tool with a rich visual companion, Ava opens a resizable
split panel beside the conversation. **Weather is the one implemented
renderer** today: hero conditions, feels-like / humidity / wind, a week chart
of highs, lows and precipitation, and daily rows. The panel has **Refresh**
and **Close**, and the divider between chat and panel is drag-resizable by
mouse or touch.

The numbers in that panel are not read out of Ava's prose. If anything fails
the artifact is simply omitted; it never blocks or breaks a normal reply.

??? note "How the panel is built, and why that matters"

    The panel is **not** parsed out of the model's prose. The bridge reads the
    `get_weather` tool-call arguments the agent actually used - from the
    session jsonl lines appended since this turn started, so a stale earlier
    call can't leak in - then re-fetches structured data itself from the same
    public source the tool uses (Open-Meteo, no API key). **Refresh** re-runs
    `GET /api/artifact/weather`.

## Voice

Voice is off by default (`features.voice`). Turned off, `/api/talk` returns a
coded `voice_off` response rather than quietly recording anyway.

Turned on, the mic in the composer is a toggle: **tap it to start recording,
tap it again to send.** The placeholder reads *"Listening… tap the mic to
send"* while it is live. Every hop after that degrades rather than fails.

| Step | What happens | What happens if it breaks |
|---|---|---|
| **1. Capture** | The browser records the clip and posts it to `POST /api/talk`. | A clip under ~0.5 s comes back asking you to speak a full sentence, not an error. |
| **2. Decode** | `ffmpeg` converts whatever container the browser produced (webm/opus, mp4/aac) to 16 kHz mono PCM. | - |
| **3. Speaker gate** | Your clip is compared against your enrolled voiceprint. | Below the threshold, Ava answers *"Sorry, I only respond to the enrolled voice"* and the turn never reaches the agent. |
| **4. Transcribe** | The GPU Whisper sidecar on `:8129`, with `AVA_STT=gpu`. | Falls back to local CPU Whisper. A stopped sidecar slows voice; it never breaks it. |
| **5. Answer** | The transcript goes through the same agent path as a typed message, with the same recall and tools. | - |
| **6. Speak** | The Kokoro service returns the WAV, with `AVA_TTS=kokoro`. | Falls back to Piper. A stopped `kokoro-tts` degrades voice *quality*, never voice. |

The speaker gate uses an ECAPA embedding compared by cosine similarity, with a
default threshold of `0.40` (`voice.threshold` in `ava.yaml`).

### Enrolling and testing your voiceprint

**Setup → Agent → Voice** is the whole flow, and it is candid about the security
property at stake: with voice on and no voiceprint enrolled, the panel says
plainly that **the gate is open** and anyone can talk to Ava.

1. **Enroll your voice.** Record a few clips of natural speech in the browser,
   then **Build voiceprint**. Nothing is uploaded anywhere; it stays on this
   machine.
2. **Read the quality stats.** Seconds captured, windows used, windows
   dropped, and min/mean/max consistency, plus a **suggested threshold**
   derived from the worst surviving window. **Apply threshold** saves it.
3. **Test the gate.** Record one more clip and see its similarity scored
   against the threshold, with a plain-English verdict.

**Discard clips** if you'd rather start over.

??? note "How the voiceprint is built"

    Ava windows the speech (4 s windows, 2 s hop), drops near-silent windows,
    embeds each with ECAPA, rejects outliers more than one standard deviation
    below the mean similarity, and saves the averaged voiceprint.

## Two controls that don't do what they look like

!!! note "The header model picker sets the *fallback* model, not the one that answers"

    With the agent runtime active - the default - chat turns think with the
    **sandbox model**, configured by `nemoclaw onboard`, and bypass the
    inference router entirely. The picker steers the router's backends, which
    is the path used if the agent is stopped. The control's own tooltip says
    exactly this. For picking what actually thinks, see
    [Choosing a model](../CHOOSE_A_MODEL.md) and
    [the agent runtime](../AGENT_RUNTIME.md).

!!! note "The composer's Code mode toggle is not wired in this UI"

    Flipping it and sending a message returns a system line pointing you at
    the Control Center. Self-editing is real, but it is driven from
    **Operations → Control Center**, not from the chat composer - see
    [the agent: tools, skills & self-improvement](agent.md).

## Where to go next

- [Memory & recall](../MEMORY.md) - what gets folded into a turn, why, and how
  to read, correct or delete it.
- [On your phone (PWA)](../MOBILE.md) - installing Ava as a home-screen app.
  Voice capture works there; there are no push notifications.
- [Operations](operations.md) - the live view of turns, render jobs and the
  approvals that pause a tool call.
- [Data, memory & privacy](data.md) - where chats, uploads and generated media
  actually live on disk, and how to export or delete them.
