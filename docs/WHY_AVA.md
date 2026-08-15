# Why Ava?

One private home for every app you built, with an AI that can reach inside each
of them, on hardware you already own.

## The problem

**Writing an app used to be a project. It is a weekend now.** So you have
several: something that tracks your training, something that tracks the money, a
blog, whatever your business needed last month, a thing that runs the house.
Each one works.

None of them know about each other. And the assistants you can buy sit *outside*
all of them, answering from whatever you remember to paste into a chat box. That
gap is the whole reason this project exists. "Talk to it and it listens" is a
commodity now, shipped by everyone; **being the place your own software plugs
into is not.**

## What is Ava?

**A private, AI-native platform for the apps you own.** Every app you run becomes
a tab in one dashboard, with its own health row, its own performance chart and
its own set of tools - and the assistant in the middle can call those tools to
reach *inside* each app, on your behalf, over MCP.

Adding one is a single folder with a manifest: no change to Ava's core, no
rebuild, no pull request against this repository.

The assistant is yours end to end: your model (served locally by **vLLM /
Ollama / llama.cpp / MLX**, or through a **cloud API key**), your persona, your
skills, your memory, and per-tool egress policies naming exactly which addresses
it may reach. The agent runs sandboxed in [NemoClaw](AGENT_RUNTIME.md). Chat,
voice and governed self-editing come with it, but they are how you *use* the
platform, not what it is for.

## Who it's for

People who build their own software - increasingly with AI writing most of it -
and now have a scatter of small apps for their health, their money, their
business, their writing and their home, with nothing joining them up. Ava is the
hub for that scatter: always-on, on their **own** hardware, wired to their
**own** apps, not locked to a single cloud vendor.

## What it does

- **Turns any app you run into a tab**, a health row, a chart and a set of tools.
- **Reaches inside those apps** through the tools they advertise over MCP,
  behind a policed boundary.
- **Sizes your hardware** and names the model class it can hold, before you
  download one.
- **Shows throughput, cost, energy, jobs and per-app health** live.
- **Talks and listens** on-device, gated to *your* voice.
- **Searches the web** through a SearXNG *you* run.
- **Edits its own source**, every change awaiting your approval.
- **Studies its own activity** and parks proposals for you.
- **Remembers what matters**, and hands you the eraser.
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

- **Not another chat app.** The chat window is the way in, not the product. If
  all you want is a private chat UI over a local model, several projects do
  that in fewer moving parts. Ava is worth its complexity only if you have apps
  to plug into it.
- **Not a model.** Ava is the control layer around one. It trails the cloud
  giants on raw model IQ and polish, because its job is to put *their* models
  (or yours) to work, privately.
- **Not a NemoClaw competitor.** NemoClaw is Ava's default agent runtime, and
  Ava is the assistant built on it. Use NemoClaw alone if you want the runtime;
  use Ava if you want the stack. See [Set up the agent](AGENT_RUNTIME.md).
- **Not multi-tenant.** One install, one owner. There is no seat count, no
  billing tier, and no capability held back behind one.

## Every claim, and what backs it

| Claim | What backs it |
|---|---|
| **Your apps become its apps.** | A small manifest adds your app, no core-code changes. Ava picks up its health and metrics and generates its agent tools and egress policy. [Connect your apps](CONNECT_YOUR_APPS.md) |
| **It reaches inside them, governed.** | Tools are discovered live from the app's own MCP server. The MCP client runs host-side, so a compromised server never gets a line into the sandbox, and the agent reaches exactly two policed bridge routes and nothing else. [Connector SDK](CONNECTOR_SDK.md) |
| **It knows what your hardware can take.** | Setup reads your chip and usable memory and names the model tier it will hold, detected live, before you download anything. [Pick a model](CHOOSE_A_MODEL.md) |
| **It watches itself, and your apps.** | Tokens per second, time to first token (TTFT), cost and energy, jobs, alerts, and per-app service health and call latency. An assistant you can't observe is one you can't trust. |
| **No personality until you give it one.** | The shipped prompt covers only what Ava must *do*. How it talks is a blank field you fill in, so a fork sounds like *your* assistant. [Persona](PERSONA.md) |
| **You own it.** | Self-hosted and single-tenant, on your GPU. Conversations, files and voiceprint stay on your box. Nothing reaches a third party unless you turn it on. |
| **Your model, local by default.** | Ships a 7B model that fits a normal GPU, and swaps in one line: vLLM, Ollama, llama.cpp, LM Studio, MLX, or a cloud endpoint. [Pick a model](CHOOSE_A_MODEL.md) |
| **It edits its own source, governed.** | Changes land as git commits, every one a revert away. By default *every* change waits for you, and secrets and models are never writable. |
| **It learns without leaking.** | Local-first cycles analyse Ava's own activity and park proposals for your sign-off. Nothing self-applies. [Memory](MEMORY.md) |

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
    ==> Platform: Unified-memory NVIDIA (GB10 / Grace-Blackwell) [verified-on-device]
    ==> Platform: Apple Silicon (Mac mini / Studio / laptop) [ci-simulated]
    Warning: This hardware class is tested by simulation, not on real hardware.
    Warning: The install should work; the numbers Ava reports are unconfirmed here.
    Warning: Help fix that: python3 tools/ondevice_check.py --record
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
