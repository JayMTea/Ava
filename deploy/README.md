# Installing & running Ava

Ava is **self-hosted and single-tenant**: you run your own instance on your own
hardware, point it at your own models, and connect your own apps. This is step
one of getting started: fork (or clone) the repo, then pick the path for your
machine.

<!-- platforms:begin:install — generated from deploy/platforms.conf -->
| Your machine | Profile | Local engine | Verified on device |
|---|---|---|---|
| Unified-memory NVIDIA (GB10 / Grace-Blackwell) | `gpu` profile | vllm | :ava-check:{ title="Verified on device" } |
| Linux + discrete NVIDIA (RTX / data-centre) | `gpu` profile | vllm | :ava-close:{ title="Logic tested by simulation; numbers unconfirmed" } |
| AMD APU (Strix Halo / Ryzen AI Max) | `rocm` profile | ollama | :ava-close:{ title="Logic tested by simulation; numbers unconfirmed" } |
| AMD discrete (Radeon / ROCm) | `rocm` profile | ollama | :ava-close:{ title="Logic tested by simulation; numbers unconfirmed" } |
| Intel Arc / Xe | `cpu` profile | ollama | :ava-close:{ title="Logic tested by simulation; numbers unconfirmed" } |
| Linux, GPU present but unidentifiable | `cpu` profile | ollama | :ava-close:{ title="Logic tested by simulation; numbers unconfirmed" } |
| CPU-only Linux | `cpu` profile | ollama | :ava-check:{ title="Verified by CI on real hardware of this class" } |
| Apple Silicon (Mac mini / Studio / laptop) | bare metal | ollama | :ava-close:{ title="Logic tested by simulation; numbers unconfirmed" } |
| Windows + NVIDIA | `gpu` profile | ollama | :ava-close:{ title="Logic tested by simulation; numbers unconfirmed" } |
| Windows, no NVIDIA | `cpu` profile | ollama | :ava-close:{ title="Logic tested by simulation; numbers unconfirmed" } |
| Unrecognised platform (gating disabled) | `cloud` profile | openai | :ava-close:{ title="Logic tested by simulation; numbers unconfirmed" } |
<!-- platforms:end -->

Apple Silicon runs [bare metal with a native engine](#apple-silicon-mac-mini-studio)
because Docker can't reach the Apple GPU; every other row is
[Docker](#1-docker-recommended) with the profile shown. With just an API key, use
the `cloud` profile and no local accelerator is involved.

**That last column is a claim about evidence, not a promise about quality.** A tick
means the code has been run on real hardware of that class — either by a human who
committed the report, or by a CI job on a runner of that class. A cross means the
detection logic is tested by simulation and expected to work, but **the numbers are
unconfirmed on that hardware**. Nothing in this table is marketing: the row for the
maintainer's own machine is the only one carrying a committed on-device report.

Both this table and the fuller one in
[Hardware support](../docs/HWINFO_VALIDATION.md) render from
`deploy/platforms.conf` (`python3 -m ava_bridge.platforms --sync`), so they cannot
drift apart the way two hand-maintained tables did.

However you install, verify the wiring afterwards: open **Setup → Hardware** and
Ava should show your machine, detected automatically.

Then continue to step two, [picking Ava's brain](../docs/CHOOSE_A_MODEL.md).

---

## 1. Docker (recommended)

Requires Docker and Docker Compose v2.

The easy path — detects your hardware, picks a profile, resolves your model's
vLLM flags, and waits for the app to actually answer before saying it is done:

```bash
cd deploy && ./install.sh
```

Or pick the profile yourself. Copy it to `.env` and start; the profile selection
lives in the file, so every later `logs` / `down` / `pull` sees the same settings:

```bash
cd deploy
cp profiles/cpu.env   .env   # no GPU  -> Ollama for inference
cp profiles/gpu.env   .env   # NVIDIA GPU -> vLLM
cp profiles/cloud.env .env   # bring an API key, no local model (edit .env first)
cp profiles/full.env  .env   # everything, incl. image/video (the GPU service)
cp profiles/agent.env .env   # + full tool-using agent (opt-in, see below)

docker compose up -d
```

`install.sh` takes the same choice as an environment variable, which is the form
the landing page points people at:

```bash
cd deploy
AVA_PROFILE=full ./install.sh      # cpu | gpu | rocm | cloud | full | agent
```

Given one, `install.sh` skips detection and uses it; given none, it detects and
prints what it chose and why. What each starts (`docker compose config
--services`, not a description of it):

| Profile | Containers | What that buys |
|---|---|---|
| `cpu` | ava, ollama | chat on a local model, no GPU |
| `gpu` | ava, vllm | chat on a local model, NVIDIA |
| `rocm` | ava, ollama-rocm | chat on a local model, AMD |
| `cloud` | ava | chat against an API key you supply |
| `agent` | ava, agent, vllm | **+ the tool-using agent** that drives your connected apps |
| `full` | ava, agent, gpu-service, vllm | **+ image and video generation** |

So `full` is the superset — everything `agent` runs, plus the GPU service. Both are
opt-in for the same reason: the agent container mounts the host Docker socket,
which is **root-equivalent** on the host, and that is your call to make rather
than the installer's. the GPU service is additionally a second GPU tenant, competing for
memory with the model you chat to.

Voice is not a profile at all — it needs a build flag (`AVA_VOICE_DEPS=1`) *and*
the switch in Setup → System, both described in the **Voice** note below.

See [profiles/README.md](profiles/README.md) for what each one sets and why the
profile lives in `.env` rather than on the command line.

`install.sh` finishes by printing a **one-time claim link** — open that to create
your admin password.

If you started compose by hand instead, first-run setup is still gated: the
published port arrives through the docker bridge, so the container sees the
gateway address rather than `127.0.0.1` and cannot treat you as local. Read the
token and open the link yourself:

```bash
docker compose exec ava cat /data/data/setup_claim
# then open  http://localhost:8096/setup?claim=<token>
```

Pin `AVA_PASSWORD=...` in `deploy/.env` beforehand to skip the gate entirely.

> **On a Mac (Apple Silicon)?** Skip Docker. Docker Desktop on macOS can't pass
> the Apple GPU through, so inference in a container runs CPU-only. Use the
> bare-metal path below — see [Apple Silicon (Mac mini / Studio)](#apple-silicon-mac-mini-studio).

Good to know:

- **All state lives in one folder** (`AVA_HOME`, default `deploy/ava-data/`):
  config, chats, media, logs, models. To back up, copy that folder.

- **Password**: pin the admin password ahead of time with `AVA_PASSWORD=...` in
  `deploy/.env`; otherwise the first-run screen sets it.

- **Model**: choose one with `AVA_MODEL=...` (gpu profile) or via `ava.yaml`. The
  gpu profile defaults to `Qwen/Qwen2.5-7B-Instruct`: 14.2 GiB of BF16 weights,
  which needs ~18 GB of VRAM once a 32768-token KV cache is counted at the default
  0.90 memory share. `install.sh` sizes for that in three tiers — below 12 GB it
  falls back to the cpu profile, between 12 and 18 GB it keeps the GPU and serves
  `Qwen/Qwen2.5-3B-Instruct` instead (same tool parser, same 32k context), and at
  18 GB or more it serves the default. Pin `AVA_MODEL=` to override any of that.

    The context is not the lever to reach for on a small card: 32768 is chosen to
    clear the ~29k tokens Ava's own system prompt and tool schemas occupy, so
    shortening it breaks the agent before it saves you any memory. Scaling up is the
    deliberate act:

    | Variable | Default | Notes |
    |---|---|---|
    | `AVA_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | Any model vLLM can serve. Set it, then re-run `./install.sh` (or `./resolve-model-flags.sh --env <model> >> .env`) so its parsers follow. |
    | `AVA_VLLM_GPU_UTIL` | `0.90` | Fraction of GPU memory vLLM may take. `profiles/full.env` lowers it to `0.55`, where the GPU service shares the pool. |
    | `AVA_VLLM_MAX_LEN` | resolved | Context ceiling, **clamped to what the model actually supports** — vLLM raises rather than clamping, so asking for more than the checkpoint allows means it never boots. |
    | `AVA_VLLM_MODEL_FLAGS` | resolved | `--tool-call-parser`, `--reasoning-parser` and any model-specific boot flags, as one string. |

    You no longer pick a parser by hand. `deploy/model-flags.conf` maps a model to
    its parsers and real context length, and `deploy/resolve-model-flags.sh` is the
    only thing that reads it — `install.sh`, `local-serve.sh` and compose all go
    through it, so the container and bare-metal paths cannot disagree. A parser
    that does not match the model returns no `tool_calls` *silently*, and every
    turn then runs to timeout, so this is the one setting worth getting right.
    An unknown model family serves without tool-calling rather than guessing.
    See [docs/CHOOSE_A_MODEL.md](../docs/CHOOSE_A_MODEL.md).

- **Inference backend**: chat flows bridge → embedded router (`:8010` in-container)
  → the profile's engine. `AVA_BACKEND_URL`/`ENGINE`/`MODEL` come from
  `deploy/profiles/<profile>.env`, which you copy to `deploy/.env`. Compose has
  **no default** for them and refuses to start without them: the bridge runs
  under every profile, so any one default is wrong for the others — the old
  global `http://vllm:8002/v1` pointed the `cpu` profile at a service it never
  started. For `cloud`, `AVA_BACKEND_URL` and `AVA_MODEL` ship empty (`ENGINE` is
  already `openai`), as does `AVA_INFERENCE_KEY` — which is *not* start-guarded,
  so an empty key starts cleanly and fails on the first turn instead.

- **Agent**: the container runs the tool-less assistant by default. For the
  **full tool-using agent** (self-coding, connectors, memory) in Docker, opt into
  the `agent` profile: `cp profiles/agent.env .env && docker compose up -d`
  (that file already sets the `AVA_AGENT_ENABLED` / `AVA_AGENT_RUNTIME` /
  `AVA_ROUTER_HOST` trio, which all three have to be right together). This
  runs a separate agent container that mounts the host Docker socket, which is
  **root-equivalent** on the host; it is opt-in for that reason. Full setup and
  the security caveat: [AGENT_RUNTIME.md, Full agent in Docker](../docs/AGENT_RUNTIME.md).

- **Voice**: the default image ships `ffmpeg` for audio handling, but not the
  speech models, which are ~2 GB of torch/whisper/speechbrain. Voice enrollment
  and transcription both need those, so **neither works on the default image** —
  build with them by setting `AVA_VOICE_DEPS=1` in `deploy/.env` before `docker compose up
  -d --build`, then turn the capability on in **Setup → System → Optional
  features** (`features.voice` is off by default). Both steps are needed: the
  switch alone has nothing to run, and the deps alone leave the switch off.

- **GPU workloads**: needs the GPU service, which only the `full` profile starts. On
  any other profile the switch reports `image_down` rather than pretending.

- **Web search**: no profile provisions SearXNG or Tor, so this does **not** work
  out of the box in Docker. `AVA_WEB_SEARXNG_URL` defaults to
  `http://127.0.0.1:8888`, which inside the `ava` container is the *container's*
  loopback — not your host's — so a SearXNG you run on the host is not reachable
  at that address either. To wire it up: run SearXNG yourself, then set
  `AVA_WEB_SEARXNG_URL` to an address the container can actually reach (a compose
  service name, or `http://host.docker.internal:8888` with the matching
  `extra_hosts`). Host-side fetch stays fail-closed over Tor by default, so
  leaving Tor unprovisioned means fetch errors rather than clearnet requests —
  set `AVA_WEB_TOR=0` to accept direct egress instead.

- **GPU telemetry in the bridge container**: compose grants the GPU to the
  *inference* service only, so **Setup → Hardware** reports no GPU even on an
  NVIDIA box, and sizes its model tier from system memory instead. The panel
  says so rather than implying a driver fault. To surface the real card, drop a
  `deploy/docker-compose.override.yml` next to the compose file:

  ```yaml
  services:
    ava:
      deploy:
        resources:
          reservations:
            devices:
              - driver: nvidia
                count: all
                capabilities: ["utility"]   # telemetry only — no compute claimed
  ```

    Compose merges `docker-compose.override.yml` automatically. It is opt-in
    rather than shipped because a `devices:` reservation makes compose **refuse to
    start at all** on a host without the NVIDIA container runtime — which is every
    `cpu` and `cloud` install, and every Mac.

### Verified install (recommended)

Published images are **cosign-signed** (Sigstore keyless; the signature proves
the image came from this repo's release CI). Pull the signed image instead of
building locally by setting `AVA_IMAGE` in `deploy/.env`, and verify it first:

Replace `X.Y.Z` below with a version that exists — see the
[Releases page](https://github.com/JayMTea/Ava/releases). (`release.yml`
publishes an image only on a `v*` tag push, so a version that has not been
released yet returns 403 from GHCR.)

The two spellings are not interchangeable: the **image tag is `X.Y.Z`** while the
**git tag in the certificate identity is `vX.Y.Z`**, and the registry path is
lowercased while the certificate identity keeps the repo's case. Substituting one
value for both is why a copy-pasted verify fails.

```bash
# Verify the release image (see SECURITY.md §9 for the exact identity regex):
cosign verify ghcr.io/jaymtea/ava-bridge:X.Y.Z \
  --certificate-identity-regexp "https://github.com/JayMTea/.+/.github/workflows/release.yml@refs/tags/vX.Y.Z" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

echo "AVA_IMAGE=ghcr.io/jaymtea/ava-bridge:X.Y.Z" >> deploy/.env
docker compose pull && docker compose up -d
```

Verifying a fork's own build? Swap `jaymtea` / `JayMTea` for your owner in both
places, keeping the same lowercase-registry, cased-identity split.

One-line install on a fresh box (auto-detects GPU/CPU). Run it from inside your
clone, or point `AVA_REPO` at your fork:

```bash
git clone https://github.com/JayMTea/Ava && cd Ava/deploy && ./install.sh
# from a fork, standalone (replace `master` with your fork's default branch or a release tag):
AVA_REPO=https://github.com/<you>/ava.git bash -c "$(curl -fsSL https://raw.githubusercontent.com/<you>/ava/master/deploy/install.sh)"
```

> **Note:** `ollama`/`vllm`/`gpu-service` are upstream images (override `gpu-service`
> with `AVA_GPU_SERVICEUI_IMAGE`). The `ava/bridge` image is built locally by default,
> or set `AVA_IMAGE` to the signed published image (above). Model weights
> download on first run and carry their own licenses (surfaced at setup).

---

## 2. Bare metal with the `ava` CLI

If you would rather run it directly (for example, you already have Python and a
GPU stack):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..

./bin/ava setup              # creates AVA_HOME, generates secrets + admin password, ava.yaml
./bin/ava models pull --auto # downloads a model that fits your hardware (once, large)

# Start an inference engine — `ava up` runs the WEB APP, never an engine.
bash deploy/local-serve.sh   # NVIDIA + Docker: serves the model with vLLM
# Apple Silicon / CPU:  ollama serve  &&  ollama pull <tag>  (see CHOOSE_A_MODEL.md)

./bin/ava doctor     # verifies hardware, dirs, config, inference, services
./bin/ava up         # runs the web app on http://localhost:8096
```

The engine step is the one people skip, and skipping it produces a working web
app whose first message fails — so `ava doctor` **exits non-zero** when nothing
can serve a chat turn, which stops the `&&` chain right at the missing step.

`ava setup` prints your generated admin password (or pass `--password`).

`./bin/ava` works from the checkout with no install step. To get a plain `ava`
command on your `PATH` instead, swap the `pip install -r requirements.txt` line
for `pip install -e .` — it installs the same dependencies and adds the console
script. Keep the `-e`: Ava runs *from* this checkout, and a non-editable install
would leave it looking for `frontend/dist`, `config.example.yaml` and
`agent/install.sh` inside `site-packages`, where they are not.

A healthy run looks like this — `doctor` shows the hardware it detected, and
`up` prints the address to open:

![Terminal: ava setup and ava doctor passing with green checks, including hardware Apple M4 Max with 128 GB unified memory, then ava up printing http://localhost:8096](../docs/assets/install-1-terminal.png)

The same install end to end, narrated (sound on):

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Screen recording: installing Ava from a terminal, through setup, doctor, and up, then opening the web app">
  <source src="../docs/assets/install-tour.mp4" type="video/mp4">
  Your browser can't play video. <a href="../docs/assets/install-tour.mp4">Download the walkthrough</a>.
</video>

### Inference on bare metal

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

### Apple Silicon (Mac mini / Studio)

A Mac is **unified-memory** hardware: CPU and GPU share one RAM pool and there is
no `nvidia-smi`. **Ava does not serve vLLM on a Mac** — upstream macOS support is
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

- **`ava models pull --auto` is Apple-aware** — on a Mac it fetches an Ollama
  model sized to your memory, never the CUDA-only vLLM default
  (`Qwen/Qwen2.5-7B-Instruct`), which cannot be served on Apple Silicon.

- LM Studio and MLX also work — point the backend `base_url` at their
  OpenAI-compatible endpoint (see the Apple example in
  [`config.example.yaml`](../config.example.yaml)).

- GPU **memory** shows in the dashboard; util/temp/power read blank on Apple
  (no unprivileged API) — that's expected, not a fault.

---

## 3. Configuration

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
`$AVA_HOME/secrets/` — or supply any of them by env (`AVA_PASSWORD`,
`AVA_SECRET`, `AVA_ROUTER_TOKEN`, `AVA_INFERENCE_KEY`). Full inventory:
[SECURITY.md §4](../SECURITY.md).

---

## 4. Connecting your own apps

Wire your apps into Ava from the browser (**Setup → Connectors → Connect an
app**: paste an address, click Detect, done) or from the CLI with a connector
manifest. The step-by-step guide, with screenshots and a video of the whole
flow, is [Connect your apps](../docs/CONNECT_YOUR_APPS.md); the full manifest
reference is the [Connector SDK](../docs/CONNECTOR_SDK.md).

---

## Troubleshooting

- `ava doctor` is the first stop; it shows what's missing.
- Health check: `curl http://localhost:8096/api/health`.
- Docker logs: `docker compose logs -f ava`.
- No GPU? `cd deploy && cp profiles/cpu.env .env` (Ollama) or
  `cp profiles/cloud.env .env` (an API key), then `docker compose up -d`. The
  profile lives in `.env`, never as `--profile` on the command line.
