# Capabilities

Ava is not a model. It is the **control layer** around one: a bridge that owns
the conversation, the dashboards, your data, and the connectors, and delegates
the thinking to whatever backend you point it at — vLLM, Ollama, llama.cpp,
MLX, LM Studio, or a cloud endpoint. Nothing on this page is about model
quality; it is about what the layer around the model does for you.

This page is the map of that layer. Every line here is a one-sentence summary
of something taken apart on a child page, with the endpoint or config key that
backs it. If a capability is not documented in detail below, it is not claimed
here.

[![Ava's architecture: phone, browser and voice clients reach the FastAPI bridge over a private network; the bridge fronts a sandboxed agent runtime, a hardware-aware inference router over local models, a media engine, and drop-in connector apps](../assets/architecture.svg)](../assets/architecture.svg)

## The five views that always ship

The shell has five built-in tabs (`BUILTIN_VIEWS` in `frontend/src/App.tsx`).
They are present on every install, with no connector wired in and nothing to
enable. The active tab lives in the URL hash (`#vitals`, `#hub/system`, …), so
every view is bookmarkable and the browser's back button moves between them.

| View | What it does | Backed by | In detail |
|------|--------------|-----------|-----------|
| **Chats** | Every typed message enters through one endpoint and a server-side gate picks the pipeline: agent turn, image render, or a direct answer. Live chain of thought, file attachments, generated images, side-panel artifacts, and push-to-talk. | `POST /api/chat-stream` | [Chat, voice & creation](chat.md) |
| **Vitals** | Ava's performance across every app — spend, throughput, time-to-first-token, renders, route errors — computed from her own logged generations rather than estimated, plus energy, which is estimated from GPU wattage and labelled as such. | `/api/perf/*`, `/api/hardware` | [Vitals](vitals.md) |
| **Operations** | What is running right now, what needs a decision from you, and the Control Center where approvals, learning proposals and staged code diffs are acted on. | `/api/ops/*` plus the `/api/stream/ops` SSE feed | [Operations](operations.md) |
| **Data** | A live inventory of every store on disk — named, sized, path-stamped — with browse, export and audited delete for each one. | `/api/data/stores` | [Data, memory & privacy](data.md) |
| **Setup** | Browser-based configuration: hardware, the brain and model store, the agent runtime and its skills, connectors, voice enrollment, memory, budgets, the audit history, and system governance. | `/api/hub/*` | [Apps, devices & MCP](connectors.md), [the agent](agent.md), [Data, memory & privacy](data.md) |

Those five labels are the ones in the app's own sidebar, and they are the
labels used throughout this section.

## Connected apps become tabs

Everything else in the nav is derived, not coded. `GET /api/apps` returns one
entry per connector that declares a `ui:` block; the sidebar renders that list
directly. **Adding an app adds a tab — no change to Ava's core, no frontend
edit, no rebuild.** Drop a `connector.yaml` folder in and the tile appears.
That holds for `embed: iframe` and `embed: none`; an `embed: native` view is
compiled into the bundle by definition, so it is the one mode that does need a
frontend change (see below).

How the tab renders is the manifest's choice:

- **`embed: iframe`** — your app's own web UI, reverse-proxied same-origin
  under `/apps/<id>/` so it inherits the session cookie and the current theme.
- **`embed: none`** — no UI of its own, so Ava renders a read-only console of
  the agent actions the app exposes.
- **`embed: native`** — a React view compiled into the bundle. The core shell
  ships none of these; a connector selecting a view that is not bundled gets a
  plain "not bundled" placeholder instead of a broken tab.

Each connected app also carries its own identity color and glyph everywhere it
appears — nav tile, chat tool chip, artifact card — so an app's output never
reads as Ava's own. See [Apps, devices & MCP](connectors.md) for what a
connector buys you and where the trust boundary sits, and the
[Connector SDK](../CONNECTOR_SDK.md) for the manifest reference.

## Three processes, not one

Ava is three separate ASGI apps. They are separate because they fail, restart
and get exposed differently.

| Process | What it is | Default bind | When it runs |
|---------|-----------|--------------|--------------|
| **Bridge** | The web app and API — the SPA, the dashboards, the connector proxy, and the token-gated `/internal/*` routes the sandboxed agent calls back through. | `127.0.0.1:8096` (`server.host` / `server.port`) | always |
| **Inference router** | An OpenAI-compatible front end over the backends `ava.yaml` declares: fit-aware selection, failover, per-generation perf logging, and model leases. | `127.0.0.1:8010` (`inference.router.host` / `.port`) | always — embedded in the bridge process by default; an always-on standalone unit is detected and used instead |
| **Agent-runtime shim** | A thin HTTP wrapper around the agent runtime, for the Docker path where the sandbox needs a Docker daemon the bridge container does not have. | `:9100` inside its own container, never published to the host | only with `agent.runtime: remote` |

**Everything binds loopback by default.** The bridge and the router both
default to `127.0.0.1`; the Docker compose file publishes the bridge as
`127.0.0.1:8096:8096`; the agent shim is `EXPOSE`d to the compose network and
never to the host. Reaching Ava from your phone is a deliberate act — a
Tailscale serve, a reverse proxy, or an explicit `server.host` change — and
[Security](../../SECURITY.md) covers what to set when you do (cookie flags,
trusted proxies, the first-run claim token).

## Three optional capabilities, and "off" means off

Image and video generation, web search, and voice are switches, not
assumptions. They live in one backend registry (`ava_bridge/features.py`) that
renders the **Setup → System → Optional features** checkboxes directly, so
what the panel shows is what the code gates on.

| Switch | Default | What it needs |
|--------|---------|---------------|
| `features.image` | on | The the GPU service connector reachable |
| `features.web_search` | off | A self-hosted SearXNG, plus the guarded fetch path |
| `features.voice` | off | `requirements-voice.txt` installed (and a voiceprint, to gate who Ava listens to) |

Every gated path is checked through the same preflight, which returns two
distinguishable codes: `<key>_off` when you turned the switch off, and
`<key>_down` when the switch is on but the backing service will not answer.
That distinction is the point — a capability you chose not to enable never
surfaces as a mysterious outage, and the chat UI turns either code into a
"here's where to fix it" link.

## One data root

Everything Ava persists resolves under **`AVA_HOME`**: `data/` (memory,
chats, tokens), `logs/` (the audit ledger, performance, hardware history,
device events), `media/` (generated images and your uploads), and `secrets/`
(backend keys, the router token). On a default bare-metal install `AVA_HOME`
is the code root itself, so a fresh clone keeps the single-user `./data
./logs ./media` layout; the compose stack mounts `./ava-data` instead.

One root means backup is a folder copy, and it means the **Data** view can
show you the whole of it. [Data, memory & privacy](data.md) walks every store,
what retention reaches, and — just as important — the three stores it
deliberately does not.

## See it running

<video controls playsinline preload="metadata"
       poster="../assets/reel-poster.png"
       style="width:100%;border-radius:8px"
       aria-label="A walkthrough of Ava: a plain-language question answered from your calendar and the forecast, the connected apps, push-to-talk voice, and the Vitals dashboard">
  <source src="../assets/ava-tour.mp4" type="video/mp4">
  <track kind="captions" srclang="en" label="English"
         src="../assets/ava-tour.vtt">
  Your browser can't play video. <a href="../assets/ava-tour.mp4">Download the walkthrough</a>.
</video>

## Where to go next

The rest of this section takes each surface apart. The reference pages below
go deeper on the pieces that cut across all of them:

- [Memory & recall](../MEMORY.md) — what long-term memory is (SQLite + FTS5,
  no embeddings), how recall reaches a turn, and how to correct or delete it.
- [Running two models](../ALLOCATION.md) — what happens when an image pipeline
  and a chat model want the same GPU memory, and how leases arbitrate it.
- [Agent runtime](../AGENT_RUNTIME.md) — the sandbox, provisioning, the
  tool-less fallback, and the Docker remote-agent path.
- [Connector SDK](../CONNECTOR_SDK.md) — the manifest that turns your app into
  a tab, a health row, a set of agent tools and an egress policy.
- [On your phone (PWA)](../MOBILE.md) — installing the web app to a home
  screen, and what works there.
