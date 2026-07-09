# Pick a model: Ava's brain

Ava is model-agnostic. Its **brain** is whatever open model you point it at:
Nemotron, Llama, Qwen, or any OpenAI-compatible endpoint, running on the
hardware you just installed on. This is step two of getting started, and it
happens in the app.

Here is the whole flow on a Mac, end to end (sound on):

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Narrated screen recording: choosing the model Ava thinks with, shown on a Mac">
  <source src="../assets/choose-model-tour.mp4" type="video/mp4">
  Your browser can't play video. <a href="../assets/choose-model-tour.mp4">Download the walkthrough</a>.
</video>

---

## Ava already knows what fits

Open **Setup → Models**. The **Your hardware** panel (filled in during
[install](../deploy/README.md)) shows your chip, usable memory, and a
recommended model tier, so you never guess what your machine can run: a big
Studio handles a 70B model, a base Mac mini stays around 8B, and an NVIDIA box
is sized by its free VRAM.

![The Models tab on a Mac: Your hardware shows Apple M4 Max, 128 GB unified memory, and a workstation tier; below it, Ava's brain with an Add a model button](assets/choose-model-1-hardware.png)

## Link a model

### Step 1: Click "Add a model", pick the engine, name the model

Pick the engine that matches your machine: Ollama, LM Studio, or MLX on a Mac;
vLLM on NVIDIA; llama.cpp anywhere; or any OpenAI-compatible cloud provider.
Choosing an engine fills in its default endpoint. Then name the model you want
it to serve (for example `llama3.1:70b`).

### Step 2: Click "Test connection"

Ava sends a real turn and shows the reply and latency before you commit:

![The Add a model form: Ollama engine, llama3.1:70b, and a green test result reading Connected in 284 ms with the model's reply](assets/choose-model-2-test.png)

### Step 3: Click "Save model"

It becomes Ava's brain: the model every conversation routes through, marked
with the **brain** badge. Link several and switch which one Ava thinks with at
any time.

![Ava's brain after saving: the llama3.1:70b entry with a brain badge, and a banner reading Saved to ava.yaml](assets/choose-model-3-brain.png)

Cloud keys go to the secrets store, never into config files.

## Or let the model store fetch one

The **Model store** below downloads models sized to your detected tier, so a
24 GB machine is never handed a 70B model:

![The Model store: brain, embed, and speech models each marked downloaded, sized to the detected workstation tier](assets/choose-model-4-store.png)

The same thing from a terminal:

```bash
ava models pull --auto     # fetches a model sized to your hardware (Apple-aware)
```

---

**Next step:** [Set up the agent](AGENT_RUNTIME.md), which gives Ava its tools,
memory, and sandbox.
