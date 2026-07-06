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
```

Then open **http://localhost:8096** — the first screen prompts you to create an
admin password (nothing to hunt for in logs).

- **All state lives in one folder** (`AVA_HOME`, default `deploy/ava-data/`):
  config, chats, media, logs, models. Back it up = copy that folder.
- Pin the admin password ahead of time with `AVA_PASSWORD=...` in `deploy/.env`
  (otherwise the first-run screen sets it).
- Choose a model with `AVA_MODEL=...` (gpu profile) or via `ava.yaml`.
- **Inference endpoint**: the container chats in direct mode against
  `AVA_INFERENCE_URL`. Each profile sets a default (gpu→vllm, cpu→ollama); for
  `cloud`, set `AVA_INFERENCE_URL` + `AVA_INFERENCE_KEY` in `deploy/.env`.
- The full **tool-using agent** (self-coding, connectors) needs the OpenClaw
  runtime, which isn't bundled in this image; the container runs the tool-less
  assistant. See the agent-runtime notes in the main docs.

One-line install on a fresh box (auto-detects GPU/CPU). Run it from inside your
clone, or point `AVA_REPO` at your fork:

```bash
git clone https://github.com/<you>/ava && cd ava/deploy && ./install.sh
# or standalone:
AVA_REPO=https://github.com/<you>/ava.git bash -c "$(curl -fsSL https://raw.githubusercontent.com/<you>/ava/main/deploy/install.sh)"
```

> Note: `ollama`/`vllm`/`gpu-service` are upstream images (override `gpu-service` with
> `AVA_GPU_SERVICEUI_IMAGE`). The `ava/bridge` image is built locally from this repo.
> Model weights download on first run and carry their own licenses (surfaced at setup).

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

Ava discovers integrations from **connector manifests** (see
[docs/PACKAGING_PLAN.md](../docs/PACKAGING_PLAN.md) §5.3). Each connector declares
its health probe, metrics/perf source, egress policy, and agent actions — and the
dashboard's service matrix, performance charts, and the agent's tools all update
automatically. A connector SDK + `ava connector new <name>` scaffold are on the
roadmap.

---

## Troubleshooting

- `ava doctor` is the first stop — it shows what's missing.
- Health check: `curl http://localhost:8096/api/health`.
- Docker logs: `docker compose logs -f ava`.
- No GPU? Use `--profile cpu` (Ollama) or `--profile cloud` (an API key).
