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
- **See everything.** A live Command Center: throughput, cost and energy, jobs, alerts.
- **Set up from the browser.** A guided Setup hub: pick a model, provision the agent, wire in apps, enroll your voice. No terminal required.
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
| Voice (+ biometric gate) | ● | ◐ | ○ | ○ | ● | ◐ |
| GPU workloads (**video** via connectors) | ● | ○ | ◐ | ○ | ○ | ◐ |
| Agent tools / skills / memory | ● | ● | ◐ | ● | ◐ | ● |
| **Self-editing** (governed code changes) | ● | ◐ | ○ | ◐ | ○ | ○ |
| Drives your **other apps** (connectors) | ● | ◐ | ○ | ◐ | ● | ◐ |
| Ops **dashboard** (perf / cost / alerts) | ● | ○ | ○ | ○ | ◐ | ○ |
| Governance / approval gates | ● | ◐ | ○ | ○ | ◐ | ◐ |
| Raw model quality (IQ) | ◐\* | ◐\* | ◐\* | ◐\* | ◐\* | ● |
| Polish / mobile / scale | ○ | ◐ | ◐ | ◐ | ● | ● |

\* inherited from whatever model you plug in.

**The honest read:** Ava is the only column that is Strong across voice, generation,
agent, self-editing, connectors, and observability *together*, self-hosted. It
trails the cloud giants on raw model IQ and polish because it is the **control
layer, not the brain**. Its job is to put *their* models (or yours) to work, privately.

**A note on NemoClaw:** it is less a competitor than a foundation. NemoClaw is
Ava's **default agent runtime** (sandbox, tools, egress policies, memory), and
Ava layers private on-device voice, GPU workloads, the connector SDK,
the ops dashboard, and governed self-editing on top. Use NemoClaw alone if
you want a channel-based agent (Slack/Telegram/etc.); use Ava if you want the
full private assistant stack. See [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md).

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

From there the **Setup hub** (in the app) walks you through the rest: detect
hardware, download a fitting model, provision the agent, wire in your apps,
enroll your voice. Prefer bare metal? `ava setup && ava doctor && ava up`;
`ava verify` then proves every advertised capability end-to-end. Full guide:
[deploy/README.md](deploy/README.md).

## Add your own app (connectors)

```bash
ava connector new myapp                 # scaffold a manifest
# edit connector.yaml: health probe, perf log, actions
ava connector tools    myapp --write    # generate the agent tools
ava connector policies myapp --write    # generate its egress policy
cd agent && ./install.sh                # deploy — zero core-code changes
```

Ava then monitors your app in the dashboard, charts its performance, and can call
its actions natively. See the [Connector SDK](docs/CONNECTOR_SDK.md).

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
