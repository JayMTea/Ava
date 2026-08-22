<div align="center">

# Ava

### Connect an app. Get its tools, its dashboard, and its firewall.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/self--hosted-yes-success.svg)](deploy/README.md)
[![Engine](https://img.shields.io/badge/engine-vLLM%20%7C%20Ollama%20%7C%20cloud-orange.svg)](docs/CHOOSE_A_MODEL.md)

**Anyone can write an app now. Nobody has a place to put them all.**

You have a training log, something that tracks the money, a blog, whatever your
business needed last month, a thing that runs the house. Each one works, none of
them know about each other, and the assistants you can buy sit *outside* all of
them, guessing from whatever you paste into a chat box.

</div>

## One manifest, six surfaces

**This is the part that is not available anywhere else.** Drop a folder with a
`connector.yaml` into your data root and Ava derives, at load time, from that one
declaration:

| Surface | What it is |
|---|---|
| **A tab in the sidebar** | Your app's own web UI, reverse-proxied same-origin under `/apps/<id>/` so it inherits your session cookie and the current theme. No second login. |
| **A health row** | On Operations → Service health, from `service.probe`. Name the feature flag governing the service and a dead probe reads *off* rather than *down*. |
| **A live perf source** | Charted on Vitals from `perf.path` — and if your app never writes a `performance.jsonl`, the bridge writes one *for* it: every proxied call timed with its latency and HTTP status, self-registering, so a brand-new app appears in Vitals on its first call with no restart. |
| **Agent tools** | Declared statically, discovered live from an `ava-tools/1` facade, or read off your app's MCP server. Generated as `.mjs` into the sandbox for you. |
| **An egress policy** | The allow-list of exactly which addresses the agent's sandbox may reach on this app's behalf, namespaced `ava-<id>`, rendered from the same file. Anything not on it is refused. |
| **Cost and energy attribution** | Per-app call count and energy on the Connected apps tile, so you can see which of the things you built is the expensive one. |

Plenty of tools are MCP clients. A whole category of MCP gateways does policy.
Every self-hosted chat UI does tools. **Deriving the UI surface, the
observability and the network policy from one declaration is the thing Ava does
that they do not** — and none of it is hand-maintained in Ava's core, so adding
your app needs no fork, no rebuild and no pull request against this repo.

Full reference: [Apps, devices & MCP](docs/capabilities/connectors.md) and the
[Connector SDK](docs/CONNECTOR_SDK.md).

## And it is all yours

It runs on hardware you already own, on the model you picked, with the persona
you wrote, the skills you gave it, and a [NemoClaw](docs/AGENT_RUNTIME.md)
sandbox around the agent. The MCP client runs **host-side**, so a compromised
tool server never gets a line into that sandbox; the agent reaches exactly two
policed bridge routes (`__tools` / `__call`) and nothing else. Nothing leaves
your machine unless you wire it out.

<div align="center">

[![Ava's architecture: clients (phone, browser, voice) reach the FastAPI bridge over a private Tailscale network; the bridge fronts a sandboxed agent runtime, a hardware-aware inference router over local models, and drop-in connector apps](docs/assets/architecture.svg)](docs/assets/architecture.svg)

</div>

---

## It tells you what your hardware can take

Running your own AI means living inside your own memory budget, so Ava starts
there. It reads your chip and usable memory and names the size class of model
your machine can actually hold — *before* you download one
([Pick a model](docs/CHOOSE_A_MODEL.md)) — then keeps throughput, time-to-first-token,
cost, energy, GPU and every connected app's health on screen while it works
([Vitals](docs/capabilities/vitals.md)). The hardware bubble on every view names
which model is Ava's brain, and lists everything *else* on the machine holding
model memory under a heading that says whose it is.

Where a claim about *your* hardware is unverified, it says so rather than
rounding up. The installer prints your platform's **verification tier** before it
does anything else, so you learn on first contact whether your box is one
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
cp profiles/gpu.env .env      # or: cpu | cuda | rocm | cloud | full | agent
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
generate, memory and voice reachable, no drift against what's committed.
It exits non-zero if a hard check fails. Treat it as a wiring-and-drift check,
not a runtime proof — an optional capability you haven't set up warns rather
than fails. (No install step? `./bin/ava` does the same thing without touching
your environment.) Full guide: [deploy/README.md](deploy/README.md).

## What it does

- **Centralize the apps you already built** — the six derived surfaces above, one manifest each. This is the reason to run Ava rather than a chat UI.
- **Answer across them.** Point Ava at any MCP server and its tools are discovered live, so a question about your training log, your ledger and your house is one question instead of three tabs, and every reply names the calls it actually made.
- **Be your apps' ops console, not just your AI's.** Spend, speed, errors and service health per connected app, computed from Ava's own logs rather than estimated.
- **Size your hardware, then watch it.** Ava names the model class your machine can hold before you download one, and keeps GPU, memory, throughput, cost and energy on screen while it runs.
- **Talk to it.** On-device voice, speech in and speech out, gated to *your* voice.
- **Search the web without being the product.** Web search goes through a SearXNG *you* run — no third party, no API key — and host-side fetch is fail-closed over Tor by default (`AVA_WEB_TOR=0` opts out), with every redirect hop re-validated against an SSRF guard. The sandboxed agent never reaches the internet directly. Note: the shipped Docker profiles do not yet provision SearXNG or Tor, so this one is opt-in setup rather than on out of the box — see [deploy/README.md](deploy/README.md).
- **It remembers — and you hold the eraser.** Long-term memory distilled from your chats and uploads, recalled when relevant, every recall audit-logged; view, correct, delete, or export all of it in the Hub ([docs/MEMORY.md](docs/MEMORY.md)).
- **See everything.** A live **Vitals** dashboard: throughput, cost and energy, jobs, alerts.
- **Set up from the browser.** A guided Setup hub: pick and download a model, wire in your apps, set budgets, toggle optional capabilities — no terminal. Two of the steps need one first: the agent runtime wants the NemoClaw CLI already installed (Ava deliberately will not run a `curl | bash` installer on your behalf), and voice in Docker needs an image built with `AVA_VOICE_DEPS=1`.
- **Take it with you.** The web app installs to your phone's home screen as a PWA ([docs/MOBILE.md](docs/MOBILE.md)).
- **Private by default.** Runs on your hardware; nothing leaves unless you say so.

## What is Ava?

**A private, AI-native platform for the apps you own.** Most AI tools are *one*
thing: a chat UI, a local model runner, a voice assistant, an agent framework,
or an MCP gateway — and every one of them sits outside your apps. Ava is the
layer your apps plug *into*, and the plug is one file (see the table above).

The assistant is yours end to end: your model (served locally by **vLLM /
Ollama / llama.cpp / MLX**, or through a **cloud API key**), your persona, your
skills, your memory, your egress policies, running sandboxed on hardware you
own. Chat and voice come with it — but they are how you
use the platform, not what it is for.

**Being honest about what is not special:** self-hosted chat over a local model,
speaking MCP, on-device voice, and egress policy for tool servers are all
commodity in 2026, shipped by Open WebUI, LibreChat, AnythingLLM, Goose, Home
Assistant and a whole category of MCP gateways. Ava does all four because a
platform needs them, not because they are a reason to pick it. The reason is the
derivation above.

## Vitals, Operations, and Data

Ava's dashboard is the front door: a **Vitals** tab (performance at a glance:
tokens/sec, time-to-first-token, cost and energy, hardware), an
**Operations** tab (live jobs, connectors, service health, alerts), and a
**Data** tab — the transparency page a
cloud assistant can't give you: every store Ava keeps (memories, chats, the
audit ledger, logs, media, even the secrets it will never display), sized and
inventoried, with search, export (per-chat or one everything-archive), audited
deletes, and database maintenance. Your whole Ava is one folder (`AVA_HOME`);
the Data tab shows you exactly what's in it.

## Why Ava?

- **One declaration, six surfaces.** UI, observability *and* network policy from
  the same manifest. MCP clients give you tools. MCP gateways give you policy.
  Deriving all three from one file, with nothing hand-maintained in Ava's core,
  is the claim worth checking against anything else you are considering
  ([docs/CONNECT_YOUR_APPS.md](docs/CONNECT_YOUR_APPS.md)).
- **The AI hub is the ops console for your software.** Per-app call counts,
  latency, service health and energy attribution, so the layer that drives your
  apps is also the layer that tells you how they are doing. Most assistants have
  no concept of "your app's health" at all.
- **It has no personality until you give it one.** The prompt Ava ships with
  covers only what it must *do* — call its tools rather than answer around
  them. How it talks is a blank field you fill in, so a fork sounds like
  *your* assistant rather than like whoever wrote this repo
  ([docs/PERSONA.md](docs/PERSONA.md)).
- **You own it.** Self-hosted and single-tenant, on your GPU. Your conversations,
  files, and voiceprint stay on your box by default. Nothing goes to a third
  party unless you turn it on, and the only such switch is a cloud inference
  backend. Ava ships with no third-party model API key of its own.
- **Your model, local by default.** Ava defaults to a 7B model that fits a normal
  GPU (downloaded on first run) and swaps in one line — run vLLM, Ollama,
  llama.cpp, or point it at a cloud endpoint. Memory distillation runs on that
  same local router with no cloud fallback at all — its prompts quote your
  conversations, so they never leave the box ([docs/MEMORY.md](docs/MEMORY.md)).
- **It knows your machine.** It names the model class your hardware can hold
  before you download one, and the hardware bubble on every view says which
  model is Ava's brain and what *else* is holding model memory
  ([docs/CHOOSE_A_MODEL.md](docs/CHOOSE_A_MODEL.md)).
- **It watches itself, and your apps.** A real operations dashboard: tokens/sec,
  TTFT, **cost and energy**, running jobs, alerts, and per-app service health
  and call latency. An assistant you can't observe is one you can't trust.
- **The MCP boundary is real.** The MCP client runs host-side, so a compromised
  server never gets a line into the sandbox — and Ava withholds the host
  environment from a stdio server rather than handing it over, or runs it in a
  throwaway container (`sandbox: docker`). The server itself is a host process
  you declared, the same trust model as any MCP desktop client; what is
  contained is the agent.

## Where it stands

Most self-hosted stacks give you a chat UI and stop there — your apps stay
outside it. Ava puts your apps, governed agent tools, voice, and
hardware-and-cost observability behind one dashboard. It trails the cloud giants
on raw model IQ and polish because it is the **control layer, not the brain**:
its job is to put *their* models (or yours) to work across *your* software,
privately.

**A note on NemoClaw:** it is less a competitor than a foundation. NemoClaw is
Ava's **default agent runtime** (sandbox, tools, egress policies, memory), and
Ava layers private on-device voice, the connector SDK and the ops
dashboard on top. Use NemoClaw alone if
you want the sandboxed agent runtime to build on; use Ava if you want the
full private assistant stack. See [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md).

## Who it's for

People who build their own software — increasingly with AI writing most of it —
and now have a scatter of small apps for their health, their money, their
business, their writing and their home, with nothing joining them up. Ava is the
hub for that scatter: always-on, on their **own** hardware, wired to their
**own** apps, not locked to a single cloud vendor.

## Add your own app (connectors)

Wire an app in from the browser: **Setup → Connectors → Connect an app**, paste
its address, click Detect, done. Ava then monitors it in the dashboard, charts
its performance, and can call its actions natively, with every app behind an
auto-generated egress policy. Step-by-step guide:
[Connect your apps](docs/CONNECT_YOUR_APPS.md). Building your own connector?
See the [Connector SDK](docs/CONNECTOR_SDK.md).

## Under the hood

A FastAPI **bridge** (web app, API, dashboard) fronts a **pluggable agent
runtime** (tools, skills, memory: [NemoClaw](docs/AGENT_RUNTIME.md) by default,
sandboxed with per-tool egress policies) and an OpenAI-compatible **inference
router** that fronts whichever engine you point it at. Everything else, the apps
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
