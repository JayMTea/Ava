# Capabilities

Ava is not a model. It is the **control layer** around one: it owns the
conversation, your data and the connectors, and delegates the thinking to
whatever backend you point it at - vLLM, Ollama, llama.cpp, MLX, LM Studio, or
a cloud endpoint. Nothing on this page is about model quality. It is about what
the layer around the model does for you.

The short version of that: **three built-in tabs, and then one more for every
app you connect.** The three are Ava's own shell. The rest of the sidebar is
your software, and it is the half that makes this worth running - see
[Connected apps become tabs](#connected-apps-become-tabs) below, and
[what one manifest gives you](connectors.md#what-one-manifest-gives-you) for the
surfaces a single `connector.yaml` derives.

## The tabs that always ship

Three built-in tabs (`BUILTIN_VIEWS` in `frontend/src/App.tsx`). They are on
every install, with no connector wired in and nothing to enable. A fourth,
**Domains**, is built in but off by default (`features.domains`).

| Tab | What it does |
|---|---|
| **[Chats](chat.md)** | Ask, attach a file, or speak. The one place you talk to the agent: every send goes through the bridge's turn pipeline, and the server owns the turn from there. You see her reasoning as it happens — streamed live when the gateway is up — and which tools she used. |
| **[Agent](agent-console.md)** | The agent's own console: the sessions it has open, what it already did, and what it runs on a schedule. You watch and operate here, you talk in Chats — your own conversations appear under *Your chats* and link back. Present whether or not you use the gateway runtime — it simply says so when there is nothing to show. |
| **[Setup](connectors.md)** | Browser-based configuration in six tabs: an overview, hardware and budgets, the agent (its runtime, brain, providers, persona, skills, memory and voice), connectors, branding, and system governance. |

[The agent](agent.md) has a reference page of its own, covering what runs
underneath rather than what you click: its tools, its skills, its runtimes, and
the rules governing what it may change.

The active tab lives in the URL hash (`#agent`, `#hub/system`), so every view
is bookmarkable and the browser's back button moves between them.

!!! note "Vitals, Operations and Data were removed"

    Three further tabs shipped until recently: **Vitals** (tokens/sec, TTFT,
    spend and energy), **Operations** (live turns, service health, alerts) and
    **Data** (the on-disk store inventory, with export and audited deletes).
    They were removed along with their `/api/perf/*`, `/api/ops/*` and
    `/api/data/*` routes.

    What produced their numbers is untouched, because it is not only theirs:
    `perf_log.py` still writes one record per generation, the hardware sampler
    still runs, and the audit ledger still records every governed action. The
    agent's own `read_performance` tool still answers "how fast is Ava
    generating?" in chat, and `ava attest` still inventories every store.

??? note "The endpoint behind each tab"

    | Tab | Backed by |
    |---|---|
    | Chats | `POST /api/chat-stream`, always. Progress streams over the `/ws/gateway` relay when the gateway runtime is live, and polls `GET /api/turn/<id>` otherwise |
    | Agent | `POST /api/gateway/rpc` plus the `/ws/gateway` event relay |
    | Setup | `/api/hub/*` |

    Those labels are the ones in the app's own sidebar, and they are the
    labels used throughout this section.

## Connected apps become tabs

Everything else in the nav is derived, not coded. `GET /api/apps` returns one
entry per connector that declares a `ui:` block, and the sidebar renders that
list directly. **Adding an app adds a tab. No change to Ava's core, no
frontend edit, no rebuild.** Drop a `connector.yaml` folder in and the tile
appears.

Each connected app carries its own identity colour and glyph everywhere it
appears - nav tile, chat tool chip, artifact card - so an app's output never
reads as Ava's own.

??? note "How the tab renders, and the one mode that needs a rebuild"

    How the tab renders is the manifest's choice:

    - **`embed: iframe`** - your app's own web UI, reverse-proxied same-origin
      under `/apps/<id>/` so it inherits the session cookie and the current
      theme.
    - **`embed: none`** - no UI of its own, so Ava renders a read-only console
      of the agent actions the app exposes. When the connector's tools are
      discovered at run time (`mcp:` or `actions.discover`), that list is
      fetched from the app as the tile opens: it is what the app actually
      serves, with the tier Ava will enforce for each tool, and it degrades to
      the last known list — labelled as such — when the app is unreachable.
    - **`embed: native`** - a React view compiled into the bundle. The core
      shell ships none of these; a connector selecting a view that is not
      bundled gets a plain "not bundled" placeholder instead of a broken tab.

    The no-rebuild claim holds for `embed: iframe` and `embed: none`. An
    `embed: native` view is compiled into the bundle by definition, so it is
    the one mode that does need a frontend change.

See [Apps, devices & MCP](connectors.md) for what a connector buys you and
where the trust boundary sits, and the [Connector SDK](../CONNECTOR_SDK.md)
for the manifest reference.

## Three processes, not one

!!! info "Nothing is reachable from your network until you say so"

    Ava runs as three separate processes, and **every one of them binds to
    localhost by default**. The web app answers on `127.0.0.1:8096` and the
    agent's helper container is never published to the host at all. Reaching
    Ava from your phone is a deliberate act - a Tailscale serve, a reverse
    proxy, or an explicit `server.host` change - and
    [Security](../../SECURITY.md) covers what to set when you do.

[![Ava's architecture: phone, browser and voice clients reach the FastAPI bridge over a private network; the bridge fronts a sandboxed agent runtime, a hardware-aware inference router over local models, and drop-in connector apps](../assets/architecture.svg)](../assets/architecture.svg)

??? note "The three processes, their bind addresses, and why they are separate"

    Ava is three separate ASGI apps (ASGI is the Python interface a web server
    speaks to a Python app). They are separate because they fail, restart and
    get exposed differently.

    | Process | What it is | Default bind | When it runs |
    |---------|-----------|--------------|--------------|
    | **Bridge** | The web app and API - the SPA, the dashboards, the connector proxy, and the token-gated `/internal/*` routes the sandboxed agent calls back through. | `127.0.0.1:8096` (`server.host` / `server.port`) | always |
    | **Inference router** | An OpenAI-compatible front end over the backends `ava.yaml` declares: fit-aware selection, failover, per-generation perf logging, and model leases. | `127.0.0.1:8010` (`inference.router.host` / `.port`) | always - embedded in the bridge process by default; an always-on standalone unit is detected and used instead |
    | **Agent-runtime shim** | A thin HTTP wrapper around the agent runtime, for the Docker path where the sandbox needs a Docker daemon the bridge container does not have. | `:9100` inside its own container, never published to the host | only with `agent.runtime: remote` |

    The bridge and the router both default to `127.0.0.1`; the Docker compose
    file publishes the bridge as `127.0.0.1:8096:8096`; the agent shim is
    `EXPOSE`d to the compose network and never to the host. What to set when
    you do expose Ava - cookie flags, trusted proxies, the first-run claim
    token - is in [Security](../../SECURITY.md).

## Two optional capabilities, and "off" means off

Web search and voice are switches, not assumptions. They live in one backend
registry (`ava_bridge/features.py`) that renders the **Setup → System →
Optional features** checkboxes directly, so what the panel shows is what the
code gates on.

| Switch | Default | What it needs |
|--------|---------|---------------|
| `features.web_search` | off | A self-hosted SearXNG, plus the guarded fetch path |
| `features.voice` | off | `requirements-voice.txt` installed (and a voiceprint, to gate who Ava listens to) |
| `features.remote_hardware` | off | node_exporter and a GPU exporter on the machine that runs your models, when that is not the box Ava is on; addresses in Setup → Hardware ([install reference](../INSTALL_REFERENCE.md#reading-the-hardware-of-another-machine)) |

A capability you chose not to enable never surfaces as a mysterious outage.

??? note "How off-by-choice is kept distinct from broken"

    Every gated path is checked through the same preflight, which returns two
    distinguishable codes: `<key>_off` when you turned the switch off, and
    `<key>_down` when the switch is on but the backing service will not
    answer. The chat UI turns either code into a "here's where to fix it"
    link, derived from the code pattern in `frontend/src/lib/fixes.ts`, so a
    new capability needs no frontend change.

## One data root

Everything Ava persists resolves under **`AVA_HOME`**. One root means backup
is a folder copy, and it means the **Data** view can show you the whole of it.

| Folder | What is in it |
|---|---|
| `data/` | memory, chats, tokens |
| `logs/` | the audit ledger, performance, hardware history, device events |
| `media/` | the files you upload |
| `secrets/` | backend keys, the router token |

On a default bare-metal install `AVA_HOME` is the code root itself, so a fresh
clone keeps the single-user `./data ./logs ./media` layout; the compose stack
mounts `./ava-data` instead.

`ava attest` inventories every one of those stores, with its size and its
resolved path - see [Proving it](../EVIDENCE.md). Retention is set in
**Setup → System**; [Memory & recall](../MEMORY.md) covers what Ava remembers
and how to correct or delete it.

## Where to go next

The rest of this section takes each surface apart. The reference pages below
go deeper on the pieces that cut across all of them:

- [Memory & recall](../MEMORY.md) - what long-term memory is (SQLite + FTS5,
  no embeddings), how recall reaches a turn, and how to correct or delete it.
- [Running two models](../ALLOCATION.md) - what happens when a second heavy
  model and the one you chat to want the same GPU memory, and how leases
  arbitrate it.
- [Agent runtime](../AGENT_RUNTIME.md) - the sandbox, provisioning, the
  tool-less fallback, and the Docker remote-agent path.
- [Connector SDK](../CONNECTOR_SDK.md) - the manifest that turns your app into
  a tab, a health row, a set of agent tools and an egress policy (the rule
  that says which hosts that app is allowed to reach).
- [On your phone (PWA)](../MOBILE.md) - installing the web app to a home
  screen, and what works there.
