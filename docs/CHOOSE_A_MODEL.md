# Pick a model: Ava's brain

Ava is model-agnostic. Its **brain** is whatever open model you point it at:
Nemotron, Llama, Qwen, or any OpenAI-compatible endpoint, running on the
hardware you just installed on. This is step two of getting started, and it
happens in the app.

---

The whole flow takes about a minute; here it is end to end, narrated (sound on):

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Screen recording: picking Ava's brain, from detected hardware through linking and testing a model">
  <source src="../assets/choose-model-tour.mp4" type="video/mp4">
  Your browser can't play video. <a href="../assets/choose-model-tour.mp4">Download the walkthrough</a>.
</video>

*(The recording uses sample data on a Mac host; your instance will show your own
hardware and models.)*

## Ava already knows what fits

Open **Setup → Hardware**. The **Your hardware** panel shows your chip, usable
memory, and a recommended model tier — detected live each time you open it,
nothing to configure during [install](../deploy/README.md) — so you never guess
what your machine can run: a big Studio handles a 70B model, a base Mac mini
stays around 8B, and an NVIDIA box is sized by its total VRAM.

![The Hardware tab: a Workstation Tier hero card reading "Unified memory comfortably fits large local models", with Compute and Usable memory below it, and a note that the tier sets which models Ava recommends](assets/choose-model-1-hardware.png)

## Link a model

Models live under **Setup → Agent**, in the **Ava's brain** panel.

### Step 1: Click "Add a model", pick the engine, name the model

Pick the engine that matches your machine. Choosing one fills in its default
endpoint; then name the model you want it to serve (for example `llama3.1:70b`).

**Ava supports any OpenAI-compatible endpoint, but it does not do the same amount
for each one** — and this used to read as though it did, listing six engines as
peers when three of them had a preset URL and nothing else. What Ava actually
provides, per engine:

<!-- engines:begin — generated from ava_bridge/engines.py -->
| Engine | Support | Health | Launcher | Weights | Token counts |
|---|---|---|---|---|---|
| **vLLM** | first-class | `/models` | deploy/local-serve.sh (and the `vllm` compose service) | ava models pull (HuggingFace cache) | yes |
| **Ollama** | first-class | `/api/tags` | the `ollama` / `ollama-rocm` compose services | ava models pull --auto (ollama pull) | yes |
| **llama.cpp** | first-class | `/health` | bring your own llama-server (no Ava launcher yet) | ava models pull (GGUF store) | not reported (unverified) |
| **Cloud (OpenAI-compatible)** | first-class | `/models` | bring your own | bring your own | yes |
| **MLX (Apple Silicon)** | generic | `/models` | mlx_lm.server --model <id> --port 8080 (documented, unverified) | huggingface cache (mlx-community/*) | not reported (unverified) |
| **LM Studio** | generic | `/models` | bring your own | bring your own | not reported (unverified) |
<!-- engines:end -->

**first-class** means Ava can start it (or says plainly that it cannot), knows
where its weights come from, and health-checks it during setup.
**generic** means it works — anything OpenAI-compatible works — but you launch and
tune it yourself.

Two honest notes on that table:

- **MLX is generic, not first-class, and that is a repo-state limitation rather
  than a judgement about MLX.** Promoting it needs a CI job on Apple hardware,
  which needs a public repository. Until then, Ollama is the supported Mac path:
  it ships a Metal build and Ava launches it.
- **LM Studio is generic deliberately and permanently.** It is a desktop GUI, so
  it cannot be installed headless, scripted from an installer, or given a
  reproducible launcher — and its endpoint is already covered by pointing the
  cloud/OpenAI engine at `http://127.0.0.1:1234/v1`. Building it a launcher would
  be inventing evidence.

Where the "Token counts" column says *not reported (unverified)*, streamed replies
from that engine will show no tokens/sec or cost. That is a deliberately
conservative choice: sending `stream_options` to an engine that rejects unknown
parameters costs the whole turn, while omitting it costs only the statistics.

### Step 2: Click "Test connection"

Ava sends a real turn and shows the reply and latency before you commit.

![The Add a model form on the Agent tab, with the engine picked, the model named, and the live result of Test connection shown beneath it](assets/choose-model-2-test.png)

### Step 3: Click "Save model"

It is marked with the **brain** badge, and you can link several and switch which
one Ava thinks with at any time. With the agent runtime off, this is the model
every conversation routes through; with it on (the default), chat turns think
with the agent sandbox's own model — set with `nemoclaw onboard` — and this
backend serves the tool-less fallback and the other model roles. The
**Setup → Agent** panel labels which one is actually in effect.

Saving writes the backend to `ava.yaml`, and Ava raises a restart banner —
restart to apply the change (`cd deploy && docker compose restart ava` under
Docker, or `./bin/ava up` to restart the service you started it with).

Cloud keys go to the secrets store, never into config files.

![The saved model listed under Ava's brain, carrying the brain badge alongside its engine and endpoint](assets/choose-model-3-brain.png)

## Or let the model store fetch one

The **Model store** below downloads models sized to your detected tier, so a
24 GB machine is never handed a 70B model.

![The Model store panel: brain, embed, and speech entries each marked downloaded, with a Pull recommended button and the detected tier and memory in the subtitle](assets/choose-model-4-store.png)

The same thing from a terminal:

```bash
ava models pull --auto     # fetches a model sized to your hardware (Apple-aware)
```

## Serving your own model on vLLM (NVIDIA)

If you run vLLM yourself rather than Ollama or a cloud endpoint, `deploy/local-serve.sh`
starts the container for whatever model you name:

```bash
AVA_MODEL=Qwen/Qwen3-32B-AWQ bash deploy/local-serve.sh
```

Set `AVA_MODEL` in `.env` to make it the default. Then point Ava at it — Setup →
Agent → **Ava's brain**, or the `inference.backends` block in `ava.yaml`. The
`model:` there has to match what the server was started with, since the router
sends that string verbatim as the OpenAI `model` field.

**The one thing to get right: the tool-call parser.** vLLM needs
`--tool-call-parser` to match the format your model emits, and a mismatch does not
raise an error — vLLM simply returns no `tool_calls`, the call arrives as ordinary
text, the agent never sees it, and turns run until they time out. It looks like Ava
is ignoring you.

`local-serve.sh` resolves the parser from the model family (Nemotron, Qwen, Llama,
Mistral, DeepSeek, GPT-OSS, GLM) so you normally don't think about it. Confirm what
it picked before committing to a large download:

```bash
AVA_SERVE_DRY_RUN=1 AVA_MODEL=Qwen/Qwen3-32B-AWQ bash deploy/local-serve.sh
```

That prints the resolved flags and exits without touching Docker — it won't disturb
a model you already have running. For a family it doesn't recognise it warns and
serves *without* tool calling rather than guessing wrong. Check the model card's
vLLM section and set the parsers yourself:

```bash
AVA_TOOL_PARSER=hermes AVA_REASONING_PARSER= bash deploy/local-serve.sh
```

An empty value means "pass no flag"; leaving a variable unset means "use the table".
Until you've confirmed tool calling works, set `tools: none` on that backend in
`ava.yaml` so tool turns route elsewhere instead of failing silently.

A text-only model is fine — Ava's vision and audio capabilities degrade gracefully
rather than breaking. Drop `vision` and `audio` from that backend's `fit.workloads`
so those turns aren't routed to a model that can't serve them.

---

**Next step:** [Set up the agent](AGENT_RUNTIME.md), which gives Ava its tools,
memory, and sandbox.
