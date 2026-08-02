# Install reference

This is the long half of installing Ava. It is for you if one of these is true:

- Ava is already running and you now want to change something - the model, the
  memory share, the engine, where the GPU shows up.
- You need a path the one-line installer does not cover: bare metal, Apple
  Silicon, a fork, or picking the container set by hand.
- Something did not behave and you want to know what the installer actually did.

If you have not installed yet, start with the
[Quickstart](../deploy/README.md) instead. It is one command, and it writes
everything below for you.

A **profile** here means which set of containers Ava starts. **Provisioning** a
capability means switching it on and giving it the service it needs.

---

## 1. Pick the profile yourself

The installer detects your hardware and picks one. To choose instead, copy a
profile file to `.env` and start. The selection lives in the file, so every later
`logs` / `down` / `pull` sees the same settings:

```bash
cd deploy
cp profiles/cpu.env   .env   # no GPU  -> Ollama for inference
cp profiles/cuda.env  .env   # small NVIDIA GPU -> Ollama with CUDA (quantized)
cp profiles/gpu.env   .env   # NVIDIA GPU, 12 GB or more -> vLLM
cp profiles/rocm.env  .env   # AMD GPU or APU -> Ollama with ROCm
cp profiles/cloud.env .env   # bring an API key, no local model (edit .env first)
cp profiles/full.env  .env   # everything, incl. image/video (the GPU service)
cp profiles/agent.env .env   # + full tool-using agent (opt-in)

docker compose up -d
```

`install.sh` takes the same choice as an environment variable, which is the form
the landing page points people at:

```bash
cd deploy
AVA_PROFILE=full ./install.sh      # cpu | cuda | gpu | rocm | cloud | full | agent
```

Given one, `install.sh` skips detection and uses it; given none, it detects and
prints what it chose and why. What each starts (`docker compose config
--services`, not a description of it):

| Profile | Containers | What that buys |
|---|---|---|
| `cpu` | ava, ollama | chat on a local model, no GPU |
| `cuda` | ava, ollama-cuda | chat on a local model, on a small NVIDIA GPU |
| `gpu` | ava, vllm | chat on a local model, NVIDIA |
| `rocm` | ava, ollama-rocm | chat on a local model, AMD |
| `cloud` | ava | chat against an API key you supply |
| `agent` | ava, agent, vllm | **+ the tool-using agent** that drives your connected apps |
| `full` | ava, agent, gpu-service, vllm | **+ image and video generation** |

So `full` is the superset - everything `agent` runs, plus the GPU service.

!!! warning "`agent` and `full` grant root-equivalent access to your machine"
    The agent container mounts the host Docker socket (`/var/run/docker.sock`),
    which is **root-equivalent** on the host. That is how it spawns the sandbox
    that runs model-generated code. Both profiles are opt-in for that reason: it
    is your call to make rather than the installer's. the GPU service is additionally a
    second GPU tenant, competing for memory with the model you chat to.

Voice is not a profile at all - it needs a build flag (`AVA_VOICE_DEPS=1`) *and*
the switch in Setup → System. See [Optional capabilities](#4-optional-capabilities) below.

??? note "What each profile file sets, and why the profile lives in `.env`"
    Compose reads `COMPOSE_PROFILES` from `.env`, so selecting the profile *in the
    file* means every later command - `logs`, `down`, `pull`, `exec` - sees the same
    environment as `up` did. `--profile` is per-invocation: a `docker compose down`
    typed without it silently interpolates different values and can fail to find the
    services it is meant to stop.

    It also fixes the defect these files were added for. The `ava` service starts
    under every profile, so its `AVA_BACKEND_URL` could only have one global default
    (`http://vllm:8002/v1`) - and under `--profile cpu`, where no `vllm` service
    exists, that pointed the bridge at a hostname nothing would ever answer. The
    backend settings live with the profile that provides them now, and compose
    refuses to start rather than interpolate a default that cannot be right for
    every profile.

    | Profile file | Serves inference with | Notes |
    |---|---|---|
    | `cpu.env` | Ollama, on the CPU | Slow but starts anywhere. `install.sh` picks this for a box with no GPU, for one whose largest GPU has under 4 GB of VRAM, and for one whose Docker daemon cannot reach the card. |
    | `cuda.env` | Ollama with CUDA, on an NVIDIA GPU | Quantized GGUF weights (`llama3.2`, Q4_K_M, about 2 GiB) rather than FP16, so it fits where vLLM does not. `install.sh` picks this between **4096 MiB** and **12000 MiB** of VRAM - the 6-8 GB laptop cards that used to be sent to `cpu`. Needs the NVIDIA Container Toolkit, same as `gpu`. |
    | `gpu.env` | vLLM on an NVIDIA GPU | Needs ~18 GB of VRAM for the shipped default model at the default 0.90 memory share (~12 GB for the 3B it downshifts to), and the NVIDIA container toolkit installed (`nvidia-smi` alone is not enough - the daemon needs the `nvidia` runtime, which `install.sh` checks for). |
    | `rocm.env` | Ollama with ROCm, on an AMD GPU or APU | Needs the amdgpu kernel driver loaded (so `/dev/kfd` and `/dev/dri` exist) and your user in the `render` and `video` groups. `install.sh` checks both and falls back to `cpu` rather than starting a service that cannot see the device. **Not verified on real AMD hardware** - the `linux-amd-*` rows in `deploy/platforms.conf` read `ci-simulated`. |
    | `cloud.env` | Someone else's API | **You must fill in three values** - the file ships them empty on purpose, so compose stops with an instruction instead of guessing. |
    | `agent.env` | vLLM + the tool-using agent | Sets `COMPOSE_PROFILES=agent,gpu`, because `agent` alone starts no inference engine. Also sets the three variables the agent runtime needs. |
    | `full.env` | vLLM + the GPU service + the agent | Everything. Shares one GPU pool, so it lowers the vLLM memory share to `0.55`. |

---

## 2. The model, and the knobs that size it

Choose a model with `AVA_MODEL=...` (gpu profile) or via `ava.yaml`. The gpu
profile defaults to `Qwen/Qwen2.5-7B-Instruct`: 14.2 GiB of BF16 weights, which
needs ~18 GB of VRAM once a 32768-token KV cache is counted at the default 0.90
memory share. `install.sh` sizes for that in four tiers, and each threshold is
arithmetic the script spells out in a comment rather than a remembered number:

| Detected GPU memory | What the installer does |
|---|---|
| under 4096 MiB | falls back to the `cpu` profile - too little to hold even a quantized 3B on the card |
| 4096 to 12000 MiB | switches to the `cuda` profile: Ollama with CUDA on quantized GGUF weights, which fit where FP16 does not. Falls back to `cpu` if Docker has no `nvidia` runtime registered |
| 12000 to 18000 MiB | keeps vLLM and serves `Qwen/Qwen2.5-3B-Instruct` instead (same tool parser, same 32k context) |
| 18000 MiB or more | serves the default |

Pin `AVA_MODEL=` to override any of that.

The context is not the lever to reach for on a small card: 32768 is chosen to
clear the ~29k tokens Ava's own system prompt and tool schemas occupy, so
shortening it breaks the agent before it saves you any memory. Scaling up is the
deliberate act:

| Variable | Default | Notes |
|---|---|---|
| `AVA_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | Any model vLLM can serve. Set it, then re-run `./install.sh` (or `./resolve-model-flags.sh --env <model> >> .env`) so its parsers follow. |
| `AVA_VLLM_GPU_UTIL` | `0.90` | Fraction of GPU memory vLLM may take. `profiles/full.env` lowers it to `0.55`, where the GPU service shares the pool. |
| `AVA_VLLM_MAX_LEN` | resolved | Context ceiling, **clamped to what the model actually supports** - vLLM raises rather than clamping, so asking for more than the checkpoint allows means it never boots. |
| `AVA_VLLM_MODEL_FLAGS` | resolved | `--tool-call-parser`, `--reasoning-parser` and any model-specific boot flags, as one string. |

You no longer pick a parser by hand. `deploy/model-flags.conf` maps a model to
its parsers and real context length, and `deploy/resolve-model-flags.sh` is the
only thing that reads it - `install.sh`, `local-serve.sh` and compose all go
through it, so the container and bare-metal paths cannot disagree. A parser
that does not match the model returns no `tool_calls` *silently*, and every
turn then runs to timeout, so this is the one setting worth getting right.
An unknown model family serves without tool-calling rather than guessing.
See [Pick a model](CHOOSE_A_MODEL.md).

---

## 3. Inference backend wiring (Docker)

Chat flows bridge → embedded router (`:8010` in-container) → the profile's
engine. `AVA_BACKEND_URL`/`ENGINE`/`MODEL` come from
`deploy/profiles/<profile>.env`, which you copy to `deploy/.env`. Compose has
**no default** for them and refuses to start without them: the bridge runs
under every profile, so any one default is wrong for the others - the old
global `http://vllm:8002/v1` pointed the `cpu` profile at a service it never
started. For `cloud`, `AVA_BACKEND_URL` and `AVA_MODEL` ship empty (`ENGINE` is
already `openai`), as does `AVA_INFERENCE_KEY` - which is *not* start-guarded,
so an empty key starts cleanly and fails on the first turn instead.

---

## 4. Optional capabilities

### Voice

The default image ships `ffmpeg` for audio handling, but not the speech models,
which are ~2 GB of torch/whisper/speechbrain. Voice enrollment and transcription
both need those, so **neither works on the default image** - build with them by
setting `AVA_VOICE_DEPS=1` in `deploy/.env` before `docker compose up -d
--build`, then turn the capability on in **Setup → System → Optional features**
(`features.voice` is off by default). Both steps are needed: the switch alone has
nothing to run, and the deps alone leave the switch off.

### GPU workloads

Needs the GPU service, which only the `full` profile starts. On any other profile the
switch reports `image_down` rather than pretending.

### Web search

No profile provisions SearXNG or Tor, so this does **not** work out of the box in
Docker. `AVA_WEB_SEARXNG_URL` defaults to `http://127.0.0.1:8888`, which inside
the `ava` container is the *container's* loopback - not your host's - so a
SearXNG you run on the host is not reachable at that address either. To wire it
up: run SearXNG yourself, then set `AVA_WEB_SEARXNG_URL` to an address the
container can actually reach (a compose service name, or
`http://host.docker.internal:8888` with the matching `extra_hosts`). Host-side
fetch stays fail-closed over Tor by default, so leaving Tor unprovisioned means
fetch errors rather than clearnet requests - set `AVA_WEB_TOR=0` to accept direct
egress instead. **Egress** here means traffic leaving your machine.

---

## 5. Surfacing the GPU in the bridge container

Compose grants the GPU to the *inference* service only, so **Setup → Hardware**
reports no GPU even on an NVIDIA box, and sizes its model tier from system memory
instead. The panel says so rather than implying a driver fault. To surface the
real card, drop a `deploy/docker-compose.override.yml` next to the compose file:

```yaml
services:
  ava:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: ["utility"]   # telemetry only - no compute claimed
```

Compose merges `docker-compose.override.yml` automatically. It is opt-in rather
than shipped because a `devices:` reservation makes compose **refuse to start at
all** on a host without the NVIDIA container runtime - which is every `cpu` and
`cloud` install, and every Mac.

---

## 6. Bare metal: engine wiring and tool calling

The bare-metal install steps are on the
[Quickstart](../deploy/README.md), collapsed under "Install bare metal instead".
This is what happens after them.

Chat flows **bridge → router → your engine**. The OpenAI-compatible router
starts **inside `ava up` automatically** (embedded, `127.0.0.1:8010`), so you
never need a second service. An always-on standalone unit
(`uvicorn ava_router:app --host 127.0.0.1 --port 8010`) is detected at startup
and used instead. Declare your engine in `ava.yaml`:

```yaml
inference:
  primary: local
  backends:
    local:
      engine: ollama                      # vllm | ollama | llamacpp | openai
      base_url: http://127.0.0.1:11434/v1
      model: llama3.2
```

`ava models pull --auto` downloads a model sized to your hardware and prints
this stanza for you. `ava doctor` checks the route chat *actually* uses.

**Tool calling per engine** (matters for the full agent; plain chat works
regardless):

| Engine | Tool calls | Launch requirement |
|---|---|---|
| vLLM | native | `--enable-auto-tool-choice --tool-call-parser <parser for your model>` (both are required; wrong parser = tools silently return as prose) |
| Ollama | native | none (tool-capable models only) |
| llama.cpp | opt-in | `llama-server --jinja` with a tool-call chat template; otherwise declare `tools: none` on the backend |
| cloud (openai) | native | none |

**Exposing the router beyond localhost**: set `inference.router.host: 0.0.0.0`.
Every `/v1/*` call then requires the router token
(`$AVA_HOME/secrets/router_token`) as a `Bearer` / `X-Ava-Router-Token`
header. Loopback (the default) needs no token for `/v1/*`.

---

## 7. Apple Silicon (Mac mini / Studio)

A Mac is **unified-memory** hardware: CPU and GPU share one RAM pool and there is
no `nvidia-smi`. **Ava does not serve vLLM on a Mac** - upstream macOS support is
experimental, build-from-source and CPU-only unless you add the community
`vllm-metal` plugin. Run bare metal with a native engine (Ollama, llama.cpp, MLX,
LM Studio) so inference uses the Metal GPU. The hardware layer detects
Apple Silicon automatically and gates model routing on the shared RAM pool (a
512 GB Studio runs a 70B comfortably; a 24 GB mini should stay ~8B).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt              # no CUDA/vLLM wheels; clean on arm64

# a native, OpenAI-compatible engine (any of these work; Ollama shown):
brew install ollama && ollama serve &
ollama pull llama3.1:70b                      # sized to YOUR Mac's memory

./bin/ava setup      # on a Mac this seeds an Ollama backend, not the vLLM default
./bin/ava doctor     # confirms it detects Apple Silicon + the right memory tier
./bin/ava up         # http://localhost:8096
```

Notes:

- **`ava models pull --auto` is Apple-aware** - on a Mac it fetches an Ollama
  model sized to your memory, never the CUDA-only vLLM default
  (`Qwen/Qwen2.5-7B-Instruct`), which cannot be served on Apple Silicon.

- LM Studio and MLX also work - point the backend `base_url` at their
  OpenAI-compatible endpoint (see the Apple example in
  [`config.example.yaml`](../config.example.yaml)).

- GPU **memory** shows in the dashboard; util/temp/power read blank on Apple
  (no unprivileged API) - that is expected, not a fault.

---

## 8. Configuration: `ava.yaml` and secrets

Everything is driven by **`$AVA_HOME/ava.yaml`** (copied from
[`config.example.yaml`](../config.example.yaml) by `ava setup`) plus environment
overrides. **No source edits, ever.** Highlights:

| Setting | What it does |
|---|---|
| `server.port` | web app port (default 8096); env `AVA_PORT` |
| `inference.backends` | your model engines (vLLM / Ollama / llama.cpp / cloud) |
| `agent.sandbox` | the agent runtime sandbox name |
| `features.*` | `image`, `web_search`, `voice` (Setup → System → Optional features), plus `learning` and `memory`, which have their own panels |
| `connectors` | the apps Ava monitors and drives |

Secrets live outside `ava.yaml` and outside the repo: the admin password in
`$AVA_HOME/data/auth_password`, the session signing key in
`$AVA_HOME/data/.secret`, the router token and cloud API key under
`$AVA_HOME/secrets/` - or supply any of them by env (`AVA_PASSWORD`,
`AVA_SECRET`, `AVA_ROUTER_TOKEN`, `AVA_INFERENCE_KEY`). Full inventory:
[SECURITY.md §4](../SECURITY.md).

---

## 9. Installing from a fork

Installing **from a fork**, without cloning it first? Point `AVA_REPO` at your
fork and pipe its own copy of the installer (it is the same script the
[Quickstart](../deploy/README.md) runs):

```bash
# replace `master` with your fork's default branch or a release tag:
AVA_REPO=https://github.com/<you>/ava.git bash -c "$(curl -fsSL https://raw.githubusercontent.com/<you>/ava/master/deploy/install.sh)"
```

Verifying a fork's own signed build is covered under "Verify the signed image"
on the [Quickstart](../deploy/README.md).

---

Back to the funnel: [Step 1: Install](../deploy/README.md) ·
[Step 2: Pick a model](CHOOSE_A_MODEL.md)
