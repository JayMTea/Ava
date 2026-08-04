# Why Ava?

A private AI hub that runs on the box you already own, and tells you which parts
of that claim it has actually verified on your hardware.

## What is Ava?

Most AI tools are *one* of these: a chat UI, a local model runner, a voice
assistant, or an agent framework. **Ava is the layer that ties them together**
into one assistant you actually own: chat, voice, app automation, and
self-editing, behind a single dashboard, running on your own hardware with the
model of your choice (served locally by **vLLM / Ollama / llama.cpp**, or
through a **cloud API key**).

## Who it's for

Privacy-conscious prosumers and small teams who want an always-on AI that runs
on their **own** hardware, connects to their **own** apps, and isn't locked to a
single cloud vendor.

## What it does

- **Talks and listens** on-device, gated to *your* voice.
- **Drives your other apps**, not just charts them.
- **Fronts any MCP tool server** behind a policed boundary.
- **Searches the web** through a SearXNG *you* run.
- **Edits its own source**, every change awaiting your approval.
- **Studies its own activity** and parks proposals for you.
- **Remembers what matters**, and hands you the eraser.
- **Shows throughput, cost, energy and jobs** live.
- **Installs to your phone** as a home-screen app.

Each of those is taken apart, with the endpoint or config key behind it, in
[Using Ava](capabilities/index.md).

!!! note "What needs a setup step first"
    **Web search** is off by default, and the shipped Docker profiles do not
    provision SearXNG or Tor yet, so it is opt-in setup rather than on out of
    the box. **Voice** is off by default, and under Docker needs an image built
    with `AVA_VOICE_DEPS=1`. **The agent runtime** wants the NemoClaw CLI
    already installed; Ava deliberately will not run a `curl | bash` installer
    on your behalf. See [Quickstart](../deploy/README.md).

!!! note "Three words this site uses a lot"
    **MCP** is the Model Context Protocol, the open standard a tool server
    speaks so any assistant can call it. A **sandbox** is the locked-down
    container Ava's agent runs inside. An **egress policy** is that sandbox's
    allow-list of what it may talk to; everything not on the list is refused.

## What it is not

- **Not a model.** Ava is the control layer around one. It trails the cloud
  giants on raw model IQ and polish, because its job is to put *their* models
  (or yours) to work, privately.
- **Not a NemoClaw competitor.** NemoClaw is Ava's default agent runtime, and
  Ava is the assistant built on it. Use NemoClaw alone if you want the runtime;
  use Ava if you want the stack. See [Set up the agent](AGENT_RUNTIME.md).
- **Not multi-tenant.** One install, one owner. There is no seat count, no
  billing tier, and no capability held back behind one.

## Why Ava?

| Claim | What backs it |
|---|---|
| **No personality until you give it one.** | The shipped prompt covers only what Ava must *do*. How it talks is a blank field you fill in, so a fork sounds like *your* assistant. [Persona](PERSONA.md) |
| **You own it.** | Self-hosted and single-tenant, on your GPU. Conversations, files and voiceprint stay on your box. Nothing reaches a third party unless you turn it on. |
| **Your model, local by default.** | Ships a 7B model that fits a normal GPU, and swaps in one line: vLLM, Ollama, llama.cpp, LM Studio, MLX, or a cloud endpoint. [Pick a model](CHOOSE_A_MODEL.md) |
| **It does more than talk.** | It calls tools, remembers, and reaches into your other apps. |
| **It watches itself.** | Tokens per second, time to first token (TTFT), cost and energy, jobs, alerts, service health. An assistant you can't observe is one you can't trust. |
| **It edits its own source, governed.** | Changes land as git commits, every one a revert away. By default *every* change waits for you, and secrets and models are never writable. |
| **It learns without leaking.** | Local-first cycles analyse Ava's own activity and park proposals for your sign-off. Nothing self-applies. [Memory](MEMORY.md) |
| **Anyone can extend it.** | A small manifest adds your app, no core-code changes. Ava picks up its health and metrics and generates its agent tools and egress policy. [Connect your apps](CONNECT_YOUR_APPS.md) |
| **MCP, but governed.** | The MCP client runs host-side, so a compromised server never gets a line into the sandbox. The agent reaches exactly two policed bridge routes and nothing else. [Connector SDK](CONNECTOR_SDK.md) |

??? note "The config keys behind those claims"
    Every default below is read straight out of the code, not out of a brochure.

    | Key | Default | What it means |
    |---|---|---|
    | `persona.style` | *empty* | Free text written straight into the prompt. Empty means the model's own voice, unshaped. Max 4000 characters. |
    | `features.learning_cloud_fallback` | `false` | Learning prompts quote your chats verbatim, so sending one to Anthropic when the local model can't finish a cycle is its own decision, and it defaults to no. Registered in `ava_bridge/features.py`; env override `AVA_LEARNING_CLOUD_FALLBACK`. |
    | `features.web_search` | `false` | Web search off. Env override `AVA_WEB_SEARCH`. |
    | `features.voice` | `false` | Voice off, and it needs `requirements-voice.txt` installed. Env override `AVA_VOICE`. |
    | `code.approval` | `all` | `all` means every self-edit waits for your approval. `policy` and `none` loosen that. `ava_bridge/access_policy.py` makes the files that write this key un-writable by the agent, so Ava cannot un-gate itself. |
    | `AVA_WEB_TOR` | `1` | Host-side fetch is fail-closed over Tor. Set `0` to opt out. |
    | `sandbox: docker` (connector manifest, `mcp:` block) | *unset* | Set it on a stdio MCP server and Ava runs it in a throwaway container: `--read-only`, a tmpfs for scratch, CPU/memory/pid caps, `no-new-privileges`, and **no host filesystem mounts**. Add `network: none` to cut its network too. The Setup GUI offers this as a one-click toggle, defaulted on when Docker is available. |

    The approvals ladder is the gate worth reading twice. A tool call that
    carries `confirm:` pauses, shows you its arguments, and runs only if you
    approve. An author's `confirm:` outranks the access tier: it always asks
    and can never be granted away. Every request and every decision is written
    to the audit ledger.

## What actually leaves your machine

[![What stays on your computer and what leaves only if you switch it on: chats, memories, files, voiceprint, model weights, connected-app data and secrets never cross the line; a web search, a prompt to a cloud model, one learning cycle, a model download, and reaching Ava from your phone each cross it only behind a named switch, and each of those switches is off or unset by default](assets/egress.svg)](assets/egress.svg)

Nothing crosses that line unless you turn it on, and each crossing carries the
name of the switch that opens it. [Privacy and security](../SECURITY.md) covers
what to set when you do open one.

## The verification tiers

**Where a claim is unverified, the code says `NOT MEASURED` rather than rounding
up.**

Ava's platform table names twelve hardware profiles, and exactly one of them is
marked verified on real silicon today. So the installer prints your platform's
**verification tier** - how strong the evidence behind "this works here"
actually is - before it installs anything. `[verified-on-device]` means somebody
ran this on that hardware class and committed the report. `[ci-simulated]` means
the decision logic is tested but the numbers are not. You learn which one you
are on first contact, not after a week of trusting a number.

??? note "What verified means here"
    Verbatim from `deploy/install.sh` on the machine this was written on, and
    the line a Mac gets today:

    ```
    Platform: Unified-memory NVIDIA (GB10 / Grace-Blackwell) [verified-on-device]
    Platform: Apple Silicon (Mac mini / Studio / laptop) [ci-simulated]
      ! This hardware class is tested by simulation, not on real hardware.
      ! The install should work; the numbers Ava reports are unconfirmed here.
      ! Help fix that: python3 tools/ondevice_check.py --record
    ```

    The tiers live in `deploy/platforms.conf`, strongest first. Anything above
    `ci-simulated` must name an evidence file that exists, or the table fails
    its own test.

    | Tier | What it means |
    |---|---|
    | `verified-on-device` | A human ran `tools/ondevice_check.py` on real hardware and committed its JSON report. |
    | `ci-native` | A CI job runs the real code on real hardware of this class. |
    | `ci-simulated` | Decision logic exercised against constructed or recorded sysfs bytes. The parsing is tested; the numbers are not. |
    | `community-reported` | Someone else's `ondevice_check` report, named in the table. |
    | `unsupported` | Detected and refused, with an explanation. |

    Every capability carries the same discipline. `ava attest` writes an
    evidence bundle of what this box can and cannot demonstrate. It is
    **unsigned on purpose**: the trust model is reproducibility rather than
    authenticity. Signing your own bundle proves only that you signed it, so
    instead the bundle ships a stdlib-only `verify.py` that recomputes every
    digest offline, and a `--self-test` that mutates a byte and asserts the
    verifier actually fails. A verifier nobody has seen fail is not a verifier.

    Hardware reports are the single most useful contribution to this right now.
    `python3 tools/ondevice_check.py --record --json` on your machine produces
    exactly the evidence the support matrix is missing. Full detail:
    [Evidence bundles](EVIDENCE.md) and
    [Hardware validation](HWINFO_VALIDATION.md).

## Start here

One command on the Docker path. It detects your hardware, writes your `.env`,
and finishes by printing a one-time link that sets your admin password.

**[Install Ava](../deploy/README.md)**
