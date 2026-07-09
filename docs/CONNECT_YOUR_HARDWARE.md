# Connect your hardware

Ava runs on hardware **you** own: a Mac mini or Studio, an NVIDIA GPU box, a
DGX Spark, or even a machine with no GPU at all. This page is about wiring Ava
to that machine, so every conversation runs locally and nothing leaves it.

Here is the whole flow on a Mac, end to end (sound on):

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Narrated screen recording: connecting Ava to your own hardware, shown on a Mac">
  <source src="../assets/connect-hardware-tour.mp4" type="video/mp4">
  Your browser can't play video. <a href="../assets/connect-hardware-tour.mp4">Download the walkthrough</a>.
</video>

---

## Step 1: Ava detects what you have

Open **Setup → Models**. The **Your hardware** panel fills in automatically:
your compute (the GPU or Apple chip), your usable memory, and a recommended
model tier with a plain-language hint. There is nothing to configure; Ava reads
the machine and sizes its expectations to it.

| Your machine | What Ava detects | What that means |
|---|---|---|
| Mac mini / Studio (Apple Silicon) | unified memory shared by CPU and GPU | run a native engine (Ollama, LM Studio, MLX); a big Studio handles a 70B model, a base mini stays around 8B |
| NVIDIA GPU box | free VRAM via NVML | the `gpu` Docker profile runs vLLM out of the box |
| DGX Spark (unified-memory NVIDIA) | system memory as the model pool | verified on-device; same flow as any NVIDIA box |
| No GPU | system memory | the `cpu` profile (Ollama) or a cloud key still work |

## Step 2: Link a model (Ava's brain)

In the same tab, **Ava's brain** is where you connect a model. Click **Add a
model**, pick the engine that matches your machine (on a Mac: Ollama, LM
Studio, or MLX; on NVIDIA: vLLM; or any OpenAI-compatible cloud provider),
name the model, and click **Test connection** to prove it answers before you
commit. Cloud keys go to the secrets store, never into config files.

Click **Save model** and it becomes Ava's brain: the model every chat routes
through. You can link several and switch which one Ava thinks with.

## Step 3: Let the model store size things for you

The **Model store** below downloads models matched to your detected tier, so a
24 GB machine is never handed a 70B model. From a terminal, `ava models pull
--auto` does the same thing: it is Apple-aware and never fetches a CUDA-only
model onto a Mac.

---

## Installing on your hardware

This page assumes Ava is installed. If it is not yet:

- **Mac (Apple Silicon):** skip Docker (containers can't reach the Apple GPU);
  use the bare-metal path in
  [Quickstart: Apple Silicon](../deploy/README.md#apple-silicon-mac-mini-studio).
- **NVIDIA GPU / no GPU / cloud key:** pick the matching Docker profile in the
  [Quickstart](../deploy/README.md).

Full platform detection details, including exactly what is read on each kind of
machine: [Hardware support](HWINFO_VALIDATION.md).
