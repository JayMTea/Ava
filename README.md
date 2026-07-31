<div align="center">

# Ava

### Your private AI: self-hosted, on your hardware, wired to your world.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/self--hosted-yes-success.svg)](deploy/README.md)
[![Engine](https://img.shields.io/badge/engine-vLLM%20%7C%20Ollama%20%7C%20cloud-orange.svg)](docs/CHOOSE_A_MODEL.md)

**A private AI hub that runs on the box you already own — and tells you which
parts of that claim it has actually verified on your hardware.**

Ava puts any model to work across your voice, your apps and your creative tools,
on your machine, answering only to you. What makes it different is the second half
of that sentence: the installer prints your platform's *verification tier* before
it does anything else, so you learn on first contact whether your hardware is one
somebody has actually run this on — or one where it is only expected to work.

Verbatim from `deploy/install.sh` on the machine this was written on, and the
line a Mac gets today:

```
Platform: Unified-memory NVIDIA (GB10 / Grace-Blackwell) [verified-on-device]
Platform: Apple Silicon (Mac mini / Studio / laptop) [ci-simulated]
  ! This hardware class is tested by simulation, not on real hardware.
  ! The install should work; the numbers Ava reports are unconfirmed here.
  ! Help fix that: python3 tools/ondevice_check.py --record
```

Every capability below carries the same discipline: where a claim is unverified,
the code says NOT MEASURED rather than rounding up. `ava attest` writes an
evidence bundle of what this box can and cannot demonstrate — **unsigned on
purpose**, because the trust model is reproducibility rather than authenticity:
it ships a stdlib-only `verify.py` that recomputes every digest offline, and a
`--self-test` that proves the verifier can actually fail.

[![Ava's architecture: clients (phone, browser, voice) reach the FastAPI bridge over a private Tailscale network; the bridge fronts a sandboxed agent runtime, a hardware-aware inference router over local models, a media engine, and drop-in connector apps](docs/assets/architecture.svg)](docs/assets/architecture.svg)

</div>

---

## Quickstart

```bash
# Docker (plug-and-play) — detects your hardware and writes deploy/.env for you:
cd deploy && ./install.sh
# when it finishes it prints a one-time link — open that to set your admin password
```

(First-run setup is gated on proving you can read the server's disk, so the
plain URL is refused until you have claimed the instance. `install.sh` reads the
token off the data volume and hands you the link; if you ever need it again:
`cd deploy && docker compose exec ava cat /data/data/setup_claim`.)

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
pip install -e .                # editable — Ava runs from this checkout
ava setup                       # AVA_HOME, secrets, admin password
ava models pull --auto          # a model that fits your box (once, large)
bash deploy/local-serve.sh      # serve it — `ava up` runs the web app, not an engine
ava doctor && ava up            # doctor exits non-zero if nothing can answer yet
```

`ava verify` then checks that each advertised capability is actually *wired*:
connector manifests in lockstep with the tools and egress policies they
generate, learning and governance reachable, no drift against what's committed.
It exits non-zero if a hard check fails. Treat it as a wiring-and-drift check,
not a runtime proof — an optional capability you haven't set up warns rather
than fails. (No install step? `./bin/ava` does the same thing without touching
your environment.) Full guide: [deploy/README.md](deploy/README.md).

## What it does

- **Talk to it.** On-device voice, speech in and speech out, gated to *your* voice.
- **Create with it.** GPU workloads orchestrated by the agent (the GPU service); video via connector apps.
- **Wire it to your apps.** Drop-in connectors; Ava monitors *and* drives them.
- **Put any MCP server behind a policed boundary.** Plug into the whole MCP ecosystem; tools are discovered live, and the agent reaches them only through two allow-listed bridge routes — never the server itself.
- **Search the web without being the product.** Web search goes through a SearXNG *you* run — no third party, no API key — and host-side fetch is fail-closed over Tor by default (`AVA_WEB_TOR=0` opts out), with every redirect hop re-validated against an SSRF guard. The sandboxed agent never reaches the internet directly. Note: the shipped Docker profiles do not yet provision SearXNG or Tor, so this one is opt-in setup rather than on out of the box — see [deploy/README.md](deploy/README.md).
- **It edits its own code.** Source changes land as git commits; by default every change waits for your approval (`code.approval`).
- **It studies itself.** Periodic local-first analysis of its own activity parks improvement proposals for your sign-off; nothing self-applies.
- **It remembers — and you hold the eraser.** Long-term memory distilled from your chats and uploads, recalled when relevant, every recall audit-logged; view, correct, delete, or export all of it in the Hub ([docs/MEMORY.md](docs/MEMORY.md)).
- **See everything.** A live **Vitals** dashboard: throughput, cost and energy, jobs, alerts.
- **Set up from the browser.** A guided Setup hub: pick and download a model, wire in your apps, set budgets, toggle optional capabilities — no terminal. Two of the steps need one first: the agent runtime wants the NemoClaw CLI already installed (Ava deliberately will not run a `curl | bash` installer on your behalf), and voice in Docker needs an image built with `AVA_VOICE_DEPS=1`.
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
![Ava's Vitals dashboard: spend, energy, throughput, time-to-first-token, renders and route errors across the top, then today's budget, inference throughput by model, model routing across four models, generation performance, energy by app, and the connected-apps list](docs/assets/vitals-dashboard.png)

## Why Ava?

- **It has no personality until you give it one.** The prompt Ava ships with
  covers only what it must *do* — call its tools, never claim it rendered an image
  it didn't. How it talks is a blank field you fill in, so a fork sounds like
  *your* assistant rather than like whoever wrote this repo
  ([docs/PERSONA.md](docs/PERSONA.md)).
- **You own it.** Self-hosted and single-tenant, on your GPU. Your conversations,
  files, and voiceprint stay on your box by default. Nothing goes to a third
  party unless you turn it on: a cloud inference backend, or an Anthropic key
  for governed code changes and the cloud fallback of the learning cycle.
- **Your model, local by default.** Ava defaults to a 7B model that fits a normal
  GPU (downloaded on first run) and swaps in one line — run vLLM, Ollama,
  llama.cpp, or point it at a cloud endpoint. Your Anthropic key drives governed
  code changes. It can *also* finish a learning or memory-distillation cycle the
  local model couldn't — those prompts include chat excerpts — but that fallback
  is **off by default** (`features.learning_cloud_fallback`), so cycles stay
  on-box unless you say otherwise ([docs/MEMORY.md](docs/MEMORY.md)).
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
  Ava picks up its health and metrics automatically, and generates its egress
  policy and agent tools for you to deploy into the sandbox in one click.
- **MCP, but governed.** Point a manifest at any Model Context Protocol server
  (HTTP or stdio) and its tools go live behind an auto-generated egress policy,
  so the sandboxed agent reaches exactly two policed routes and nothing else.
  The MCP client runs host-side, so a compromised server never gets a line into
  the sandbox — and Ava withholds the host environment from a stdio server
  rather than handing it over, or runs it in a throwaway container
  (`sandbox: docker`). The server itself is a host process you declared, the
  same trust model as any MCP desktop client; what is contained is the agent.

## Where it stands

Most self-hosted stacks give you a chat UI and stop there. Ava puts voice,
generation, governed agent tools, connectors, self-editing, and cost-and-energy
observability behind one dashboard. It trails the cloud giants on raw model IQ
and polish because it is the **control layer, not the brain**: its job is to put
*their* models (or yours) to work, privately.

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

## Author

Ava is built and maintained by **Joshua Thompson** ([@JayMTea](https://github.com/JayMTea)).

Open to collaboration — if you are building something adjacent, porting Ava to
hardware I cannot test on, or want to talk about any of it:

- **Issues and discussion**: [github.com/JayMTea/Ava/issues](https://github.com/JayMTea/Ava/issues)
- **Contributing**: [CONTRIBUTING.md](CONTRIBUTING.md)
- **Security reports**: privately, per [SECURITY.md](SECURITY.md)
- **LinkedIn**: [joshua-thompson-b89913105](https://www.linkedin.com/in/joshua-thompson-b89913105)

Hardware reports are the single most useful contribution right now: Ava claims
support for four platform families and only some of them have been verified on real
silicon. `python3 tools/ondevice_check.py --record --json` on yours produces exactly
the evidence the support matrix is missing.

## Citation

If you write about Ava or build on it, GitHub's **Cite this repository** button reads
[CITATION.cff](CITATION.cff) and will give you APA or BibTeX.

## License

[Apache-2.0](LICENSE) © 2026 Joshua Thompson. Bundled models and third-party components
carry their own licenses; see [NOTICE](NOTICE).

Forking and commercial use are both fine and need no permission. Apache-2.0 asks two
things of a redistribution: keep the license and copyright notices, and carry
[NOTICE](NOTICE) along with it (§4). It grants no trademark rights (§6), so please do
not present a fork as the original — [TRADEMARK.md](TRADEMARK.md) says what that
means in practice. Short version: the code is yours, the name is not, and Ava's
built-in [branding](docs/BRANDING.md) makes calling your install something else a
two-minute job.
