<div align="center">

# Ava

### Your private AI: self-hosted, on your hardware, wired to your world.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/self--hosted-yes-success.svg)](deploy/README.md)
[![Engine](https://img.shields.io/badge/engine-vLLM%20%7C%20Ollama%20%7C%20cloud-orange.svg)](docs/CHOOSE_A_MODEL.md)

**Ava is a self-hosted personal AI operating layer:** a private, plug-and-play hub
that puts *any* model to work across your **voice**, your **apps**, and your
**creative tools**, and keeps improving itself. It runs on *your* machine, talks
to *your* apps, and answers only to you.

> Not a chatbot. Not a model. A **control layer** for your own AI.

[![Ava's architecture: clients (phone, browser, voice) reach the FastAPI bridge over a private Tailscale network; the bridge fronts a sandboxed agent runtime, a hardware-aware inference router over local models, a media engine, and drop-in connector apps](docs/assets/architecture.svg)](docs/assets/architecture.svg)

</div>

---

- **Talk to it.** On-device voice, speech in and speech out, gated to *your* voice.
- **Create with it.** GPU workloads orchestrated by the agent (the GPU service); video via connector apps.
- **Wire it to your apps.** Drop-in connectors; Ava monitors *and* drives them.
- **Wrap any MCP server in an egress policy.** Plug into the whole MCP ecosystem; tools are discovered live, and the agent reaches them only through two policed routes.
- **Search the web without being the product.** Web search runs through a loopback SearXNG and, by default, fail-closed over Tor; every redirect hop is re-validated against an SSRF guard. The sandboxed agent never reaches the internet directly.
- **It edits its own code.** Source changes land as git commits; by default every change waits for your approval (`code.approval`).
- **It studies itself.** Periodic local-first analysis of its own activity parks improvement proposals for your sign-off; nothing self-applies.
- **It remembers — and you hold the eraser.** Long-term memory distilled from your chats and uploads, recalled when relevant, every recall audit-logged; view, correct, delete, or export all of it in the Hub ([docs/MEMORY.md](docs/MEMORY.md)).
- **See everything.** A live **Vitals** dashboard: throughput, cost and energy, jobs, alerts.
- **Set up from the browser.** A guided Setup hub: pick a model, provision the agent, wire in apps, enroll your voice. No terminal required.
- **Take it with you.** The web app installs to your phone's home screen as a PWA ([docs/MOBILE.md](docs/MOBILE.md)).
- **Private by default.** Runs on your hardware; nothing leaves unless you say so.

## What is Ava?

Most AI tools are *one* of these: a chat UI, a local model runner, a voice
assistant, an agent framework, or an image generator. **Ava is the layer that ties
them together** into one assistant you actually own: chat, voice, generation, app
automation, and self-editing, behind a single dashboard, running on your own
hardware with the model of your choice (served locally by **vLLM / Ollama /
llama.cpp**, or through a **cloud API key**).

## Vitals, Operations, and Data

Ava's dashboard is the front door: a **Vitals** tab (performance at a glance:
tokens/sec, time-to-first-token, render times, cost and energy, hardware), an
**Operations** tab (live jobs, background workflows, connectors, alerts, and the
approval-gated Control Center), and a **Data** tab — the transparency page a
cloud assistant can't give you: every store Ava keeps (memories, chats, the
audit ledger, logs, media, even the secrets it will never display), sized and
inventoried, with search, export (per-chat or one everything-archive), audited
deletes, and database maintenance. Your whole Ava is one folder (`AVA_HOME`);
the Data tab shows you exactly what's in it.

<!-- Regenerate this and the site's screenshots/tour with the local capture studio. -->
![Ava's Vitals dashboard: spend, energy, throughput, time-to-first-token, renders and route errors across the top, then today's budget, inference throughput, model routing, generation performance, energy by app, and live hardware telemetry](docs/assets/vitals-dashboard.png)

## Why Ava?

- **You own it.** Self-hosted and single-tenant, on your GPU. Your conversations,
  files, and voiceprint stay on your box by default. Nothing goes to a third
  party unless you turn it on: a cloud inference backend, or an Anthropic key
  for governed code changes and the cloud fallback of the learning cycle.
- **Your model, local by default.** Ava defaults to a 7B model that fits a normal
  GPU (downloaded on first run) and swaps in one line — run vLLM, Ollama,
  llama.cpp, or point it at a cloud endpoint. Your Anthropic key drives governed
  code changes, and is the fallback when the local model can't complete a
  learning or memory-distillation cycle — those prompts include chat excerpts
  ([docs/MEMORY.md](docs/MEMORY.md)). Leave `ANTHROPIC_API_KEY` unset to keep
  every cycle local-only.
- **It does more than talk.** It renders images, calls tools, remembers,
  and reaches into your other apps.
- **It watches itself.** A real operations dashboard: tokens/sec, TTFT, render
  times, **cost and energy**, running jobs, alerts, service health. An assistant
  you can't observe is one you can't trust.
- **It edits its own source, governed.** Ava generates code changes via your Anthropic
  key and commits them to git (every change one revert away). `code.approval` picks the
  gate: by default **every** change waits for you; secrets and models are never writable.
  Separately, local-first learning cycles analyze its own activity and park improvement
  proposals; review, approve, or reject them in **Operations → Control** (the
  Control Center).
- **Anyone can extend it.** Add your app with a small manifest and no core-code changes.
  Ava picks up its health, metrics, egress policy, and agent tools automatically.
- **MCP, but governed.** Point a manifest at any Model Context Protocol server
  (HTTP or stdio) and its tools go live behind an auto-generated egress policy,
  so the sandboxed agent reaches exactly two policed routes and nothing else.
  Every other MCP client trusts the server; Ava contains it.

## Where it stands

Ava is the only self-hosted stack that puts voice, generation, agent tools,
self-editing, connectors, and observability together behind one dashboard. It
trails the cloud giants on raw model IQ and polish because it is the **control
layer, not the brain**: its job is to put *their* models (or yours) to work,
privately.

**A note on NemoClaw:** it is less a competitor than a foundation. NemoClaw is
Ava's **default agent runtime** (sandbox, tools, egress policies, memory), and
Ava layers private on-device voice, GPU workloads, the connector SDK,
the ops dashboard, and governed self-editing on top. Use NemoClaw alone if
you want the sandboxed agent runtime to build on; use Ava if you want the
full private assistant stack. See [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md).

## Who it's for

Privacy-conscious prosumers and small teams who want an always-on AI
that runs on their **own** hardware, connects to their **own** apps, and isn't
locked to a single cloud vendor.

## Quickstart

```bash
# Docker (plug-and-play) — detects your hardware and writes deploy/.env for you:
cd deploy && ./install.sh
# open http://localhost:8096 — the first screen prompts you to set an admin password
```

Or pick the profile yourself:

```bash
cd deploy
cp profiles/gpu.env .env      # or: cpu | cloud | full | agent
docker compose up -d
```

The `cloud` profile ships `AVA_BACKEND_URL`, `AVA_MODEL`, and `AVA_INFERENCE_KEY`
empty on purpose — fill them in `.env` before `docker compose up -d`.

**On a Mac (Apple Silicon)?** Run bare metal, not Docker (Docker Desktop can't use
the Apple GPU): see [Apple Silicon (Mac mini / Studio)](deploy/README.md#apple-silicon-mac-mini-studio).

From there the **Setup hub** (in the app) walks you through the rest: detect
hardware, download a fitting model, provision the agent, wire in your apps,
enroll your voice.

**Prefer bare metal?** Install the CLI into a virtualenv, then run it:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                       # editable — Ava runs from this checkout
ava setup && ava doctor && ava up
```

`ava verify` then proves every advertised capability end-to-end. (No install
step? `./bin/ava` does the same thing without touching your environment.) Full
guide: [deploy/README.md](deploy/README.md).

## Add your own app (connectors)

Wire an app in from the browser: **Setup → Connectors → Connect an app**, paste
its address, click Detect, done. Ava then monitors it in the dashboard, charts
its performance, and can call its actions natively, with every app behind an
auto-generated egress policy. Step-by-step guide, with a video:
[Connect your apps](docs/CONNECT_YOUR_APPS.md). Building your own connector?
See the [Connector SDK](docs/CONNECTOR_SDK.md).

## Under the hood

A FastAPI **bridge** (web app, API, dashboard) fronts a **pluggable agent
runtime** (tools, skills, memory: [NemoClaw](docs/AGENT_RUNTIME.md) by default,
sandboxed with per-tool egress policies) and an OpenAI-compatible **inference
router** that fronts whichever engine you point it at. GPU workloads runs on **the GPU service**;
video pipelines arrive as connector apps, not core. Everything else, the apps
Ava monitors and drives, is a **connector** you can drop in.

- **Architecture**: [system overview](docs/assets/architecture.svg)
- **Agent runtime**: [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md)

## License

[Apache-2.0](LICENSE) © The Ava Authors. Bundled models and third-party components
carry their own licenses; see [NOTICE](NOTICE).
