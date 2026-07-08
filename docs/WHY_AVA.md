<div align="center">

# Ava

### Your private Jarvis — self-hosted, on your hardware, wired to your world.

**Ava is a self-hosted personal AI operating layer:** a private, plug-and-play hub
that puts *any* model to work across your **voice**, your **apps**, and your
**creative tools** — and keeps improving itself. It runs on *your* machine, talks
to *your* apps, and answers only to you.

> Not a chatbot. Not a model. A **control layer** for your own AI.

`Apache-2.0` · `Self-hosted` · `Bring your own GPU or cloud key`

</div>

---

- 🗣️ **Talk to it** — on-device voice (speech in, speech out), gated to *your* voice.
- 🎨 **Create with it** — GPU workloads orchestrated by the agent (the GPU service); video via connector apps.
- 🧩 **Wire it to your apps** — drop-in connectors; Ava monitors *and* drives them.
- 🔌 **Wrap any MCP server in an egress policy** — plug into the whole MCP ecosystem; tools are discovered live, and the agent reaches them only through two policed routes.
- 🛠️ **It edits its own code** — generates and applies source changes as git commits; by default every change waits for your approval (`code.approval`).
- 🧠 **It studies itself** — periodic local-first analysis of its own activity parks improvement proposals for your sign-off; nothing self-applies.
- 📊 **See everything** — a live Command Center: throughput, cost/energy, jobs, alerts.
- 🖱️ **Set up from the browser** — a guided Setup hub: pick a model, provision the agent, wire in apps, enroll your voice. No terminal required.
- 🔒 **Private by default** — runs on your hardware; nothing leaves unless you say so.

## What is Ava?

Most AI tools are *one* of these: a chat UI, a local model runner, a voice
assistant, an agent framework, or an image generator. **Ava is the layer that ties
them together** into one assistant you actually own — chat + voice + generation +
app automation + self-editing, behind a single dashboard, running on your own
hardware with the model of your choice (local **vLLM / Ollama / llama.cpp**, or a
**cloud key**).

## Why Ava?

- **You own it.** Self-hosted and single-tenant, on your GPU. Your conversations,
  files, and voiceprint never leave your box.
- **Local Omni brain.** Ava's normal chat runs on the local Nemotron open-model 30B
  stack; Claude/Opus API access is reserved for governed code changes.
- **It does more than talk.** It renders images, calls tools, remembers,
  and reaches into your other apps.
- **It watches itself.** A real operations dashboard — tokens/sec, time-to-first-token,
  render times, **cost & energy**, running jobs, alerts, service health. An assistant
  you can't observe is one you can't trust.
- **It edits its own source, governed.** Ava generates code changes via your Anthropic
  key and commits them to git (every change one revert away). `code.approval` picks the
  gate: by default **every** change waits for you; secrets and models are never writable.
  Separately, local-first learning cycles analyze her own activity and park improvement
  proposals — review, approve, or reject them on the Learning page.
- **Anyone can extend it.** Add your app with a small manifest — no core-code changes.
  Ava picks up its health, metrics, egress policy, and agent tools automatically.
- **MCP, but governed.** Point a manifest at any Model Context Protocol server
  (HTTP or stdio) and its tools go live — behind an auto-generated egress policy,
  so the sandboxed agent reaches exactly two policed routes and nothing else.
  Every other MCP client trusts the server; Ava contains it.

## How it compares

Strong ● · Partial ◐ · None ○ — honest, not marketing:

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

**The honest read:** Ava is the only column that's Strong across voice + generation +
agent + self-editing + connectors + observability *together*, self-hosted. It
trails the cloud giants on raw model IQ and polish — because it's the **control
layer, not the brain**. Its job is to put *their* models (or yours) to work, privately.

**A note on NemoClaw:** it's less a competitor than a foundation — NemoClaw is
Ava's **default agent runtime** (sandbox, tools, egress policies, memory), and
Ava layers private on-device voice, GPU workloads, the connector SDK,
the ops dashboard, and governed self-editing on top. Use NemoClaw alone if
you want a channel-based agent (Slack/Telegram/etc.); use Ava if you want the
full private assistant stack. See [AGENT_RUNTIME.md](AGENT_RUNTIME.md).

## What Ava is *not*

- **Not a foundation model** — it orchestrates intelligence, it doesn't produce it.
- **Not a cloud SaaS** — there's no Ava server; you run it.
- **Not a frontier-chatbot replacement** — for raw reasoning, plug in the best model you can.

## Who it's for

Privacy-conscious tinkerers, prosumers, and small teams who want an always-on AI
that runs on their **own** hardware, connects to their **own** apps, and isn't
locked to a single cloud vendor.

## Quickstart

```bash
# Docker (plug-and-play) — pick a profile for your hardware:
cd deploy && docker compose --profile gpu up -d   # or: cpu | cloud | full
# open http://localhost:8096 — the first visit sets your admin password
```

From there the **Setup hub** (in the app) walks you through the rest — detect
hardware, download a fitting model, provision the agent, wire in your apps,
enroll your voice. Prefer bare metal? `ava setup && ava doctor && ava up`;
`ava verify` then proves every advertised capability end-to-end. Full guide →
[deploy/README.md](../deploy/README.md).

## Under the hood

A FastAPI **bridge** (web app + API + dashboard) fronts a **pluggable agent
runtime** (tools, skills, memory — [NemoClaw](AGENT_RUNTIME.md) by default,
sandboxed with per-tool egress policies) and an OpenAI-compatible **inference
router** for the local open-model model. GPU workloads runs on **the GPU service**;
video pipelines arrive as connector apps, not core.
Everything else — the apps Ava monitors and drives — is a **connector** you can
drop in. See the [architecture diagram](assets/architecture.svg) and
the [productization plan](PACKAGING_PLAN.md).
