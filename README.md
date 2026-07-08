<div align="center">

# Ava

### Your private AI: self-hosted, on your hardware, wired to your world.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/self--hosted-yes-success.svg)](deploy/README.md)
[![Model](https://img.shields.io/badge/model-vLLM%20%7C%20Ollama%20%7C%20cloud-orange.svg)](docs/PACKAGING_PLAN.md)

**Ava is a self-hosted personal AI operating layer:** a private, plug-and-play hub
that puts *any* model to work across your **voice**, your **apps**, and your
**creative tools**, and keeps improving itself. It runs on *your* machine, talks
to *your* apps, and answers only to you.

> Not a chatbot. Not a model. A **control layer** for your own AI.

[![System diagram](docs/assets/architecture.svg)](docs/assets/architecture.svg)

</div>

---

- **Talk to it.** On-device voice, speech in and speech out, gated to *your* voice.
- **Create with it.** GPU workloads orchestrated by the agent (the GPU service); video via connector apps.
- **Wire it to your apps.** Drop-in connectors; Ava monitors *and* drives them.
- **Wrap any MCP server in an egress policy.** Plug into the whole MCP ecosystem; tools are discovered live, and the agent reaches them only through two policed routes.
- **It edits its own code.** Source changes land as git commits; by default every change waits for your approval (`code.approval`).
- **It studies itself.** Periodic local-first analysis of its own activity parks improvement proposals for your sign-off; nothing self-applies.
- **It remembers — and you hold the eraser.** Long-term memory distilled from your chats and uploads, recalled when relevant, every recall audit-logged; view, correct, delete, or export all of it in the Hub ([docs/MEMORY.md](docs/MEMORY.md)).
- **See everything.** A live Command Center: throughput, cost and energy, jobs, alerts.
- **Set up from the browser.** A guided Setup hub: pick a model, provision the agent, wire in apps, enroll your voice. No terminal required.
- **Take it with you.** The web app installs to your phone's home screen as a PWA ([docs/MOBILE.md](docs/MOBILE.md)).
- **Private by default.** Runs on your hardware; nothing leaves unless you say so.

## What is Ava?

Most AI tools are *one* of these: a chat UI, a local model runner, a voice
assistant, an agent framework, or an image generator. **Ava is the layer that ties
them together** into one assistant you actually own: chat, voice, generation, app
automation, and self-editing, behind a single dashboard, running on your own
hardware with the model of your choice (local **vLLM / Ollama / llama.cpp**, or a
**cloud key**).

## The Command Center

Ava's dashboard is the front door: a **Vitals** tab (performance at a glance:
tokens/sec, time-to-first-token, render times, cost and energy, hardware) and an
**Operations** tab (live jobs, background workflows, connectors, alerts, and the
approval-gated Control Center).

<!-- Regenerate this and the site's screenshots/tour with the local capture studio. -->
![Ava's Vitals dashboard: performance at a glance](docs/assets/vitals-dashboard.png)

## Why Ava?

- **You own it.** Self-hosted and single-tenant, on your GPU. Your conversations,
  files, and voiceprint never leave your box.
- **Local Omni brain.** Ava's normal chat runs on the local Nemotron open-model 30B
  stack; Claude/Opus API access is reserved for governed code changes.
- **It does more than talk.** It renders images, calls tools, remembers,
  and reaches into your other apps.
- **It watches itself.** A real operations dashboard: tokens/sec, TTFT, render
  times, **cost and energy**, running jobs, alerts, service health. An assistant
  you can't observe is one you can't trust.
- **It edits its own source, governed.** Ava generates code changes via your Anthropic
  key and commits them to git (every change one revert away). `code.approval` picks the
  gate: by default **every** change waits for you; secrets and models are never writable.
  Separately, local-first learning cycles analyze its own activity and park improvement
  proposals; review, approve, or reject them on the Learning page.
- **Anyone can extend it.** Add your app with a small manifest and no core-code changes.
  Ava picks up its health, metrics, egress policy, and agent tools automatically.
- **MCP, but governed.** Point a manifest at any Model Context Protocol server
  (HTTP or stdio) and its tools go live behind an auto-generated egress policy,
  so the sandboxed agent reaches exactly two policed routes and nothing else.
  Every other MCP client trusts the server; Ava contains it.

## How it compares

Strong ● · Partial ◐ · None ○. Honest, not marketing:

| Capability | **Ava** | NemoClaw (OpenClaw) | Open WebUI | OpenHands | Home Assistant | ChatGPT/Claude (cloud) |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| Self-hosted / private | ● | ● | ● | ● | ● | ○ |
| Model-agnostic (bring your own) | ● | ● | ● | ◐ | ◐ | ○ |
| Chat | ● | ● | ● | ◐ | ◐ | ● |
| Voice | ● | ● | ○ | ○ | ● | ◐ |
| Biometric voice gate (speaker verification) | ● | ○ | ○ | ○ | ◐ | ○ |
| Image / video generation | ● | ● | ◐ | ○ | ○ | ◐ |
| Agent tools / skills / memory | ● | ● | ◐ | ● | ◐ | ● |
| Self-editing (governed code changes) | ● | ● | ○ | ◐ | ○ | ○ |
| Connectors / integrations | ● | ● | ○ | ◐ | ● | ◐ |
| Egress-policed connectors (per-app network policy) | ● | ◐ | ○ | ○ | ○ | ○ |
| Ops **dashboard**: chat / sessions / tasks / logs | ● | ● | ◐ | ◐ | ● | ◐ |
| Perf & energy telemetry (tokens/sec, TTFT, energy, alerts) | ● | ◐† | ○ | ○ | ◐ | ○ |
| User-facing memory governance (view / correct / delete / export) | ● | ◐ | ○ | ○ | ○ | ◐ |
| Governance / approval gates | ● | ● | ○ | ○ | ◐ | ◐ |
| Raw model quality (IQ) | ◐\* | ◐\* | ◐\* | ◐\* | ◐\* | ● |
| Polish / mobile / scale | ◐ | ● | ◐ | ◐ | ● | ● |

\* inherited from whatever model you plug in.
† OpenClaw surfaces session token and cost *estimates* in its Control UI, but does
not (yet) offer dedicated performance/energy telemetry or an alerts surface.

**The honest read:** Ava and OpenClaw overlap heavily by design — Ava runs *on*
OpenClaw. Both are self-hosted, model-agnostic, and Strong across chat, voice,
generation, memory, self-editing, and connectors. Where Ava adds value is the
layer it wraps around that shared core: **perf/energy observability**, a
**biometric speaker gate**, **per-connector egress policies**, and **user-facing
memory governance** — an ops/security/governance skin over a best-in-class agent
runtime. OpenClaw leads on maturity, polish, and community scale, and both trail
the cloud giants on raw model IQ, because Ava is the **control layer, not the brain**.

**A note on OpenClaw / NemoClaw:** it is a foundation, not a competitor. NemoClaw
(which runs OpenClaw in a sandbox) is Ava's **default agent runtime** — sandbox,
tools, egress enforcement, per-session memory — and Ava is a private-assistant
distribution built on top of it: observability, biometric voice, the connector
SDK's egress policies, and memory governance. Use OpenClaw directly if you want
the agent and its channels (Slack/Telegram/etc.); use Ava if you want that same
engine packaged as an observable, egress-policed, governed home appliance. See
[docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md).

## What Ava is *not*

- **Not a foundation model.** It orchestrates intelligence; it doesn't produce it.
- **Not a cloud SaaS.** There is no Ava server; you run it.
- **Not a frontier-chatbot replacement.** For raw reasoning, plug in the best model you can.

## Who it's for

Privacy-conscious tinkerers, prosumers, and small teams who want an always-on AI
that runs on their **own** hardware, connects to their **own** apps, and isn't
locked to a single cloud vendor.

## Quickstart

```bash
# Docker (plug-and-play) — pick a profile for your hardware:
cd deploy && docker compose --profile gpu up -d   # or: cpu | cloud | full
# open http://localhost:8096 — the first screen prompts you to set an admin password
```

**On a Mac (Apple Silicon)?** Run bare metal, not Docker (Docker Desktop can't use
the Apple GPU): see [Apple Silicon (Mac mini / Studio)](deploy/README.md#apple-silicon-mac-mini--studio).

From there the **Setup hub** (in the app) walks you through the rest: detect
hardware, download a fitting model, provision the agent, wire in your apps,
enroll your voice. Prefer bare metal? `ava setup && ava doctor && ava up`;
`ava verify` then proves every advertised capability end-to-end. Full guide:
[deploy/README.md](deploy/README.md).

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
router** for the local open-model model. GPU workloads runs on **the GPU service**;
video pipelines arrive as connector apps, not core. Everything else, the apps
Ava monitors and drives, is a **connector** you can drop in.

- **Architecture**: [system overview](docs/assets/architecture.svg)
- **Agent runtime**: [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md)
- **Productization roadmap**: [docs/PACKAGING_PLAN.md](docs/PACKAGING_PLAN.md)

## License

[Apache-2.0](LICENSE) © The Ava Authors. Bundled models and third-party components
carry their own licenses; see [NOTICE](NOTICE).
