# Installing & running Ava

Ava is **self-hosted and single-tenant** — you run your own instance on your own
hardware, point it at your own models, and connect your own apps. There are three
ways to run it, easiest first.

---

## 1. Docker (recommended — plug-and-play)

Requires Docker + Docker Compose v2. Pick the profile that matches your machine:

```bash
cd deploy

docker compose --profile cpu   up -d   # no GPU  -> Ollama for inference
docker compose --profile gpu   up -d   # NVIDIA GPU -> vLLM
docker compose --profile cloud up -d   # bring an API key, no local model
docker compose --profile full  up -d   # everything, incl. image/video (the GPU service)
docker compose --profile agent up -d   # + full tool-using agent (opt-in, see below)
```

Then open **http://localhost:8096** — the first screen prompts you to create an
admin password (nothing to hunt for in logs).

- **All state lives in one folder** (`AVA_HOME`, default `deploy/ava-data/`):
  config, chats, media, logs, models. Back it up = copy that folder.
- Pin the admin password ahead of time with `AVA_PASSWORD=...` in `deploy/.env`
  (otherwise the first-run screen sets it).
- Choose a model with `AVA_MODEL=...` (gpu profile) or via `ava.yaml`.
- **Inference backend**: chat flows bridge → embedded router (`:8010` in-container)
  → the profile's engine. Each profile sets `AVA_BACKEND_URL`/`ENGINE`/`MODEL`
  defaults (gpu→vllm, cpu→ollama); for `cloud`, set them + `AVA_INFERENCE_KEY` in
  `deploy/.env` (see the compose header).
- The container runs the tool-less assistant by default. For the **full
  tool-using agent** (self-coding, connectors, memory) in Docker, opt into the
  `agent` profile (`AVA_AGENT_ENABLED=1 AVA_AGENT_RUNTIME=remote
  AVA_ROUTER_HOST=0.0.0.0`, then `docker compose --profile agent up -d`). It runs
  a separate agent container that mounts the host Docker socket
  (**root-equivalent** — opt-in for that reason). Full setup + security caveat:
  [AGENT_RUNTIME.md → Full agent in Docker](../docs/AGENT_RUNTIME.md).

### Verified install (recommended)

Published images are **cosign-signed** (Sigstore keyless — the signature proves
the image came from this repo's release CI). Pull the signed image instead of
building locally by setting `AVA_IMAGE` in `deploy/.env`, and verify it first:

```bash
# Verify the release image (see SECURITY.md §9 for the exact identity regex):
cosign verify ghcr.io/jaymtea/ava-bridge:v0.1.0 \
  --certificate-identity-regexp "https://github.com/JayMTea/.+/release.yml@refs/tags/v0.1.0" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

echo "AVA_IMAGE=ghcr.io/jaymtea/ava-bridge:v0.1.0" >> deploy/.env
docker compose --profile gpu pull && docker compose --profile gpu up -d
```

One-line install on a fresh box (auto-detects GPU/CPU). Run it from inside your
clone, or point `AVA_REPO` at your fork:

```bash
git clone https://github.com/<you>/ava && cd ava/deploy && ./install.sh
# or standalone:
AVA_REPO=https://github.com/<you>/ava.git bash -c "$(curl -fsSL https://raw.githubusercontent.com/<you>/ava/main/deploy/install.sh)"
```

> Note: `ollama`/`vllm`/`gpu-service` are upstream images (override `gpu-service` with
> `AVA_GPU_SERVICEUI_IMAGE`). The `ava/bridge` image is built locally by default, or set
> `AVA_IMAGE` to the signed published image (above). Model weights download on
> first run and carry their own licenses (surfaced at setup).

---

## 2. Bare metal with the `ava` CLI

If you'd rather run it directly (e.g. you already have Python + a GPU stack):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..

./bin/ava setup      # creates AVA_HOME, generates secrets + admin password, ava.yaml
./bin/ava doctor     # verifies hardware, dirs, config, inference, services
./bin/ava up         # runs the web app on http://localhost:8096
```

`ava setup` prints your generated admin password (or pass `--password`).

### Inference on bare metal

Chat flows **bridge → router → your engine**. The OpenAI-compatible router
starts **inside `ava up` automatically** (embedded, `127.0.0.1:8010`) — you
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
| vLLM | native | `--tool-call-parser <parser for your model>` (wrong parser = tools silently return as prose) |
| Ollama | native | none (tool-capable models only) |
| llama.cpp | opt-in | `llama-server --jinja` with a tool-call chat template; otherwise declare `tools: none` on the backend |
| cloud (openai) | native | none |

**Exposing the router beyond localhost**: set `inference.router.host: 0.0.0.0`
— every `/v1/*` call then requires the router token
(`$AVA_HOME/secrets/router_token`) as a `Bearer` / `X-Ava-Router-Token`
header. Loopback (the default) needs no token for `/v1/*`.

---

## 3. Configuration

Everything is driven by **`$AVA_HOME/ava.yaml`** (copied from
[`config.example.yaml`](../config.example.yaml) by `ava setup`) plus environment
overrides — **no source edits, ever**. Highlights:

| Setting | What it does |
|---|---|
| `server.port` | web app port (default 8096) — env `AVA_PORT` |
| `inference.backends` | your model engines (vLLM / Ollama / llama.cpp / cloud) |
| `agent.sandbox` | the agent runtime sandbox name |
| `features.*` | toggle voice / voiceprint / web search / image |
| `connectors` | the apps Ava monitors & drives |

Secrets (admin password, signing key, API keys) live in `$AVA_HOME/secrets/` or
env vars — never in `ava.yaml`, never in the repo.

---

## 4. Connecting your own apps

Ava discovers integrations from **connector manifests**. Each connector declares
its health probe, metrics/perf source, egress policy, and agent actions — and the
dashboard's service matrix, performance charts, and the agent's tools all update
automatically:

```bash
ava connector new myapp                 # scaffold a manifest
# edit connector.yaml: health probe, perf log, actions
ava connector tools    myapp --write    # generate the agent tools
ava connector policies myapp --write    # generate its egress policy
```

Full guide → [docs/CONNECTOR_SDK.md](../docs/CONNECTOR_SDK.md).

---

## Troubleshooting

- `ava doctor` is the first stop — it shows what's missing.
- Health check: `curl http://localhost:8096/api/health`.
- Docker logs: `docker compose logs -f ava`.
- No GPU? Use `--profile cpu` (Ollama) or `--profile cloud` (an API key).
