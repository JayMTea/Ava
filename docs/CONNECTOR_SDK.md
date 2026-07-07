# Ava Connector SDK

> Add your own app to Ava by dropping in a folder — **no edits to Ava's code**.
> A connector gives your app a left-rail tile, an embedded (or native) UI, a
> health row on the Ops dashboard, a performance source, live agent tools, and an
> auto-generated egress policy — all derived from one `connector.yaml`.

This is the productization contract: fork Ava, connect **your** apps, ship.

---

## 1. Where connectors live

Manifests are discovered from two roots (the second overrides the first by id):

1. **Built-in** — `<repo>/connectors/<id>/connector.yaml` (first-party, shipped)
2. **User** — `$AVA_HOME/connectors/<id>/connector.yaml` (yours)

`$AVA_HOME` defaults to the repo root; in Docker it's the mounted data volume.
Drop a folder in, restart Ava (or `ava up`), done. Folders starting with `_`/`.`
are ignored (that's why `_template/` is skipped).

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
`AVA_DATA`, `ROOT`) and then the process
environment — so `${MYCRM_API_URL}` resolves from Ava's config/env.

---

## 3. The three embed tiers (how your UI renders)

Ava's left nav is **data-driven** from `/api/apps` (which reads the registry), so
your tile appears with no frontend edits. `ui.embed` picks how the shell renders
the app body:

| `embed` | Who it's for | How Ava renders it |
|---|---|---|
| **`iframe`** | **Third-party apps** (the common case) | Ava reverse-proxies your app's web UI **same-origin** under `/apps/<id>/` and shows it in an `<iframe>`. Because it's same-origin, your app inherits Ava's session cookie — **no separate login**. Ava's theme is passed as `?theme=light\|dark`. |
| **`native`** | First-party React views | Renders `NATIVE_VIEWS[view]`, provided by an optional gitignored overlay (`frontend/src/overlay/views/*`). Reserved for apps bundled into the frontend. |
| **`none`** | Apps with tools but no UI | Ava renders a generic **action console** listing the app's agent actions. |

### Why same-origin matters (the SSO trick)
Ava's session cookie is `httpOnly` + `SameSite=Lax` + host-only, so a raw
`http://127.0.0.1:9000` iframe would receive **no** cookie. Ava proxies your app
under its **own** origin (`/apps/<id>/`) so the cookie flows and your app is
authenticated for free. You never handle Ava's auth.

### iframe security
The iframe is sandboxed (`allow-scripts allow-forms allow-same-origin`). Because
the proxy makes it same-origin, treat the embedded app as trusted-ish; review
third-party app code before enabling it, as you would any plugin.

---

## 4. Browser data-proxy (`ui.api`)

For an app whose UI (native or iframe) needs to call **its own** backend API from
the browser without exposing a token, declare `ui.api`. Ava forwards
`/apps/<id>/api/<path>` → `base + prefix + /<path>` with the `token_env` bearer
injected server-side (the browser never sees the token). `base` defaults to the
`service.probe` host.

> **First-party note:** an app that also streams **media** (arbitrary non-API
> paths) may keep a dedicated full reverse-proxy instead — e.g. a media app with
> its own `/<app>/*` proxy. The generic `/apps/<id>/api` proxy is API-only.

---

## 5. Agent tools

Two shapes, both reached through **one** generic bridge route
(`/internal/connector/<id>/<action>`) — no bespoke bridge code:

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
Bridges an existing **MCP-style tool server** live — Ava GETs `list` for the tool
schemas and POSTs `call` `{name, arguments}` to invoke. Great for wrapping a
FastMCP/tool server without re-declaring each tool. Reserved bridge actions
`__tools` and `__call` serve this.

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
- **Agent tools** ← `actions` (declared or discovered)
- **Agent egress policy** ← `egress` + `actions`
- **Browser data-proxy** ← `ui.api`
- **Chat quick-cards** ← `chat_pickup` (after a turn used one of your tools,
  Ava reads your app's log for artifacts produced during the turn and attaches
  them as deterministic cards; app-relative URLs resolve via `/apps/<id>`)
- **Ops job attribution** ← `jobs` (your app's active jobs appear next to the
  GPU graph so spikes are self-explanatory)
- **Loaded-model roles** ← `model_hints` (label what a checkpoint is FOR)

The `chat_pickup` / `jobs` / `model_hints` field shapes are documented inline in
[`connectors/_template/connector.yaml`](../connectors/_template/connector.yaml).

---

## 7. Worked example

[`examples/hello-app/`](../examples/hello-app/) is a complete, runnable
third-party connector (iframe UI + discovered tools + health) in ~150 lines. Copy
it into `$AVA_HOME/connectors/` and watch it appear — the acceptance test for
"fork Ava, add your app, zero source edits."

## 8. First-party vs third-party tiers

- **Third-party** (you): `embed: iframe` (your own UI) or `embed: none` (tools
  only), reached entirely through the generic proxy. This is the supported,
  no-core-edit path. The first-party apps were migrated onto exactly this path as the dogfood.
- **First-party native** (in-repo): a React view in `NATIVE_VIEWS` + optionally a
  dedicated proxy for media. Reserved for apps shipped inside Ava's bundle.

See also [PACKAGING_PLAN.md §5.3](PACKAGING_PLAN.md).
