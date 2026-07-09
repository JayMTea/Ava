# Ava Connector SDK

> Add your own app to Ava by dropping in a folder, with **no edits to Ava's
> code**. A connector gives your app a left-rail tile, an embedded (or native)
> UI, a health row on the Ops dashboard, a performance source, live agent tools,
> and an auto-generated egress policy, all derived from one `connector.yaml`.

This is the productization contract: fork Ava, connect **your** apps, ship.

> **Just wiring an existing app in?** You don't need this page. Use the browser
> flow in [Connect your apps](CONNECT_YOUR_APPS.md) (Setup → Connectors →
> Connect an app); it writes this manifest for you. This page is the reference
> for everything a connector can declare.

---

## 1. Where connectors live

Manifests are discovered from two roots (the second overrides the first by id):

1. **Built-in**: `<repo>/connectors/<id>/connector.yaml` (first-party, shipped)
2. **User**: `$AVA_HOME/connectors/<id>/connector.yaml` (yours)

`$AVA_HOME` defaults to the repo root; in Docker it is the mounted data volume.
Drop a folder in, restart Ava (or `ava up`), done. Folders starting with `_`/`.`
are ignored (that is why `_template/` is skipped).

Scaffold one:

```bash
ava connector new mycrm      # writes $AVA_HOME/connectors/mycrm/connector.yaml
ava connector list           # show all loaded connectors
ava connector apps           # show the left-rail app registry
```

---

## 2. The manifest

```yaml
id: mycrm                     # unique id (defaults to the folder name)
label: My CRM
kind: app                     # core | inference | media | app

ui:                           # OPTIONAL — declare it to get a left-rail tile
  label: My CRM
  icon: panel                 # a key in frontend/src/lib/icons.tsx (unknown = no icon, safe)
  section: apps               # rail group: core | apps  (default apps)
  order: 50                   # sort order within the section
  embed: iframe               # native | iframe | none  (see §3)
  url: "http://127.0.0.1:9000"   # embed=iframe: your app's own web server
  view: mycrm                 # embed=native: a key in the frontend NATIVE_VIEWS registry
  api:                        # OPTIONAL browser data-proxy (see §4)
    prefix: "/api"
    token_env: MYCRM_API_TOKEN

service:                      # OPTIONAL — dashboard health row
  name: My CRM
  probe: "http://127.0.0.1:9000/health"
  unit: mycrm.service         # optional systemd user unit

perf:                         # OPTIONAL — a performance.jsonl source for the dashboard
  app: mycrm
  path: "${AVA_HOME}/connectors/mycrm/performance.jsonl"

auth:                         # OPTIONAL — bearer token for your app's API (static actions)
  token_env: MYCRM_API_TOKEN

egress:                       # OPTIONAL — what Ava's agent tools may reach
  routes: ["POST /internal/connector/mycrm/create_lead"]
  hosts:  ["127.0.0.1:9000"]

actions:                      # OPTIONAL — agent tools (see §5)
  static:
    - { id: create_lead, method: POST, path: "/api/leads", input: { name: {type: string} } }
  discover:
    base: "http://127.0.0.1:9000"
    list: "/tools"
    call: "/call"
    token_env: MYCRM_MCP_TOKEN
```

`${VAR}` references expand from connector vars (`AVA_HOME`, `AVA_LOGS`,
`AVA_DATA`, `ROOT`) and then the process environment, so `${MYCRM_API_URL}`
resolves from Ava's config or env.

---

## 3. The three embed tiers (how your UI renders)

Ava's left nav is **data-driven** from `/api/apps` (which reads the registry), so
your tile appears with no frontend edits. `ui.embed` picks how the shell renders
the app body:

| `embed` | Who it's for | How Ava renders it |
|---|---|---|
| **`iframe`** | **Third-party apps** (the common case) | Ava reverse-proxies your app's web UI **same-origin** under `/apps/<id>/` and shows it in an `<iframe>`. Because it's same-origin, your app inherits Ava's session cookie, so there is **no separate login**. Ava's theme is passed as `?theme=light\|dark`. |
| **`native`** | First-party React views | Renders `NATIVE_VIEWS[view]`, provided by an optional gitignored overlay (`frontend/src/overlay/views/*`). Reserved for apps bundled into the frontend. |
| **`none`** | Apps with tools but no UI | Ava renders a generic **action console** listing the app's agent actions. |

### Why same-origin matters

Ava's session cookie is `httpOnly` + `SameSite=Lax` + host-only, so a raw
`http://127.0.0.1:9000` iframe would receive **no** cookie. Ava proxies your app
under its **own** origin (`/apps/<id>/`) so the cookie flows and your app is
authenticated for free. You never handle Ava's auth.

### iframe security

The iframe is sandboxed (`allow-scripts allow-forms allow-same-origin`). Because
the proxy makes it same-origin, treat the embedded app as trusted code: review a
third-party app before enabling it, as you would any plugin.

---

## 4. Browser data-proxy (`ui.api`)

For an app whose UI (native or iframe) needs to call **its own** backend API from
the browser without exposing a token, declare `ui.api`. Ava forwards
`/apps/<id>/api/<path>` → `base + prefix + /<path>` with the `token_env` bearer
injected server-side (the browser never sees the token). `base` defaults to the
`service.probe` host.

> **First-party note:** an app that also streams **media** (arbitrary non-API
> paths) may keep a dedicated full reverse-proxy instead, for example a media app
> with its own `/<app>/*` proxy. The generic `/apps/<id>/api` proxy is API-only.

---

## 5. Agent tools

Two shapes, both reached through **one** generic bridge route
(`/internal/connector/<id>/<action>`), with no bespoke bridge code:

### Declared (`actions.static`)

Each becomes a generated tool calling your REST API through the proxy:

```yaml
actions:
  static:
    - id: create_lead
      method: POST                 # GET args -> query params; POST -> JSON body
      path: "/api/leads/{owner}"   # {tmpl} path params are filled from args
      input: { owner: {type: string}, name: {type: string} }
```

Generate the `.mjs` tools: `ava connector tools mycrm --write`.

### Discovered (`actions.discover`)

Bridges an existing **MCP-style tool server** live: Ava GETs `list` for the tool
schemas and POSTs `call` `{name, arguments}` to invoke. This wraps a
FastMCP-style tool server without re-declaring each tool. Reserved bridge actions
`__tools` and `__call` serve this.

### MCP servers (`mcp:`): wrap any Model Context Protocol server

The headline path into the roughly 20,000-server MCP ecosystem. Point the
manifest at any real MCP server and its tools are discovered and bridged live:

```yaml
mcp:
  url: "http://127.0.0.1:9200/mcp"     # Streamable HTTP transport
  token_env: MYAPP_TOKEN                # optional bearer
  # transport: sse                      # legacy HTTP+SSE (e.g. Home Assistant's
  #                                     # MCP Server) — inferred when the url
  #                                     # path ends in /sse
  # or a stdio server (spawned by the bridge):
  # command: ["npx", "-y", "@modelcontextprotocol/server-github"]
  # env: { GITHUB_TOKEN: "${GITHUB_TOKEN}" }
  # sandbox: docker                     # run the stdio server contained (below)
  # image: node:20-slim                 # base image with the runtime (default)
  # network: bridge                     # or `none` to cut the server off the net
```

**The security model: an egress policy around every MCP server.** Ava's
sandboxed agent never connects to the MCP server. The bridge speaks MCP
(JSON-RPC over Streamable HTTP or stdio) server-side, and the agent can reach
only the two policed bridge routes (`__tools`/`__call`) that the connector's
auto-generated egress policy allow-lists, and nothing else. A malicious or
compromised MCP server never gains a direct line into the sandbox, and the
sandbox never gains a direct line out.

**Container isolation for stdio servers (`sandbox: docker`).** A `command:`
server (for example `npx …`) is code you install; by default it runs as you on
the host, the same trust model as every MCP desktop client. Set `sandbox: docker`
and Ava runs it inside a throwaway container instead: `--read-only`, a tmpfs
for scratch, CPU/memory/pid caps, `no-new-privileges`, and **no host filesystem
mounts**, so an untrusted server cannot touch your files. Set `network: none`
to also cut its network. The Setup → Connect an app GUI offers this as a
one-click toggle, defaulted on when Docker is available.

**Per-action approval (`confirm`).** Gate a sensitive tool behind your explicit
OK: put `confirm: true` on a static action, or at the connector level use
`confirm: true` (every action) or `confirm: [tool_a, tool_b]`. When the agent
calls a gated action the call **pauses**, an approval prompt appears with the
arguments, and the action runs only if you approve (or is refused on deny or
timeout). Every request and decision is written to the audit ledger.

**Access tiers (JIT consent).** Every action carries a tier — explicit
`access:` on a static action, else inferred from its HTTP shape:

| Tier | At call time |
|---|---|
| `read` | runs silently |
| `write` | asks on first use; "Always allow" grants durably |
| `destructive` | asks every time — never grantable |
| `physical` | **moves something in the real world** (relay, lock, valve). Asks every time — never grantable. Never inferred: it must be declared. |

Dynamic tools (MCP / discovered) have no static declaration to infer from, so
the manifest classifies them by name pattern — first match wins:

```yaml
dynamic_access:
  GetLiveContext: read        # fnmatch patterns
  "Vacuum*": write
  "*": physical
```

Unmatched dynamic tools default to `write` — except on a `role: device`
connector, where they default to `physical`: a device's unknown verbs are
presumed to actuate until the author says otherwise. See the
[Home Assistant connector](CONNECT_HOME_ASSISTANT.md) for the worked example.

### Egress

`ava connector policies <id> --write` renders the connector's egress into
`agent/policies/generated/<id>.yaml`; `cd agent && ./install.sh` deploys it into
the sandbox. The generic proxy routes for your actions (and `__tools`/`__call`)
are allow-listed automatically.

---

## 6. What's derived automatically

From that one manifest, with nothing hand-maintained in Ava's core:

- **Left-rail tile + embedded/native UI** ← `ui`
- **Ops dashboard health row** ← `service.probe`
- **Dashboard performance source** ← `perf`
- **Agent tools** ← `actions` (declared or discovered) or `mcp` (live from the server)
- **Agent egress policy** ← `egress` + `actions` + `mcp`
- **Browser data-proxy** ← `ui.api`
- **Chat quick-cards** ← `chat_pickup` (after a turn used one of your tools,
  Ava reads your app's log for artifacts produced during the turn and attaches
  them as deterministic cards; app-relative URLs resolve via `/apps/<id>`)
- **Ops job attribution** ← `jobs` (your app's active jobs appear next to the
  GPU graph so spikes are self-explanatory)
- **Loaded-model roles** ← `model_hints` (label what a checkpoint is for)

The `chat_pickup` / `jobs` / `model_hints` field shapes are documented inline in
[`connectors/_template/connector.yaml`](../connectors/_template/connector.yaml).

---

## 7. Worked example: connect the sample app

[`examples/hello-app/`](../examples/hello-app/) is a complete, runnable
third-party connector (iframe UI, discovered tools, and a health probe) in about
150 lines. It is the acceptance test for "fork Ava, add your app, zero source
edits":

```bash
# 1. Start the app's own web server (its UI + /health + /tools + /call)
python examples/hello-app/server.py        # serves http://127.0.0.1:8477

# 2. Register it with Ava by dropping the folder into your data root
cp -r examples/hello-app "$AVA_HOME/connectors/hello"   # $AVA_HOME defaults to the repo root

# 3. Restart Ava (or `ava up`) and open the web app
#    -> "Hello App" appears in the left rail, embedded same-origin
#    -> ask Ava: "ping the hello app"  (its tools were discovered live)
```

| File | Role |
|---|---|
| `connector.yaml` | The manifest. `ui.embed: iframe` + `url` gives the rail tile and the embedded UI. `service.probe` gives the dashboard health row. `actions.discover` bridges the app's tool set. `egress` renders into the agent's network policy. |
| `server.py` | The app: a stdlib-only HTTP server exposing `/` (UI), `/health`, `/tools`, `/call`. Replace it with your real app. |

The contract the app implements: `GET /health` returns `{"ok": true}`; `GET /`
serves the UI (read `?theme=light|dark` to match Ava); tools are either
discovered (`GET /tools` + `POST /call`, as here) or declared under
`actions.static` in the manifest.

## 8. First-party vs third-party tiers

- **Third-party** (you): `embed: iframe` (your own UI) or `embed: none` (tools
  only), reached entirely through the generic proxy. This is the supported,
  no-core-edit path. The first-party apps were migrated onto exactly this path
  as the dogfood.
- **First-party native** (in-repo): a React view in `NATIVE_VIEWS`, plus
  optionally a dedicated proxy for media. Reserved for apps shipped inside
  Ava's bundle.

See also [PACKAGING_PLAN.md §5.3](PACKAGING_PLAN.md).
