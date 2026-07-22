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
  icon: grid                  # optional — a key in frontend/src/lib/icons.tsx.
                              #   Omit for a stable auto icon derived from the app
                              #   id, so apps that declare none still differ.
  color: "#7c5cff"            # optional identity accent: Ava marks everything that
                              #   belongs to your app (sidebar dot, chat tool chips,
                              #   cards) with this color. Omit for a stable auto
                              #   color derived from the app id.
                              # Both are also pickable in the GUI — Setup →
                              #   Connectors → Appearance writes them back here.
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
  path: "${AVA_LOGS}/apps/mycrm/performance.jsonl"
  # Hub-created connectors get exactly this block by default, and the bridge
  # records every proxied action call there (latency + status) — so a new app
  # shows in Vitals from its first call with no code changes. Keep the path
  # OUTSIDE $AVA_HOME/connectors/<id>: history then survives disconnect and
  # resumes when the same id is re-added. If your app writes its own SDK
  # perf log (tokens/sec, render times), point `path` at that file instead.

auth:                         # OPTIONAL — bearer token for your app's API (static actions)
  token_env: MYCRM_API_TOKEN  #   the NAME of the env var — never the value (see below)

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

### Credentials: the manifest names the token, never holds it

Every auth field above (`auth.token_env`, `mcp.token_env`, `discover.token_env`,
`ui.api.token_env`, and any `${VAR}`) is the **name of an environment variable**,
not the secret. The value is resolved on the bridge, host-side, only when a
request is about to leave for your app — so the sandboxed agent never sees it,
and it is never written to the manifest. This is the hard **Ava-never-has-
passwords** invariant.

The value can come from two places, checked in this order:

1. **A real environment variable** (`export MYCRM_API_TOKEN=…`, systemd
   `EnvironmentFile=`, Docker `environment:`) — always wins.
2. **Ava's secret store** — paste the token once in **Setup → Connectors** (the
   *Access token / API key* field on connect, or *Add credential* on an existing
   row). It's saved `0600` under `$AVA_HOME/secrets/env/<NAME>`, survives restarts
   and every **redeploy** (you're never re-prompted), and is never exported to a
   subprocess. Forkers don't have to touch `.env` at all — if you paste a value
   without naming a variable, Ava derives a stable one (`<CID>_TOKEN`).

Deploy/redeploy only regenerate tools + egress policy; they never read or ask for
a credential.

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

### Make your UI mount-agnostic (the one requirement on your app)

Embedded, your app is served from `/apps/<id>/` instead of `/` — so a UI built
with **absolute** URLs (`/assets/main.js`, `fetch('/api/things')`) breaks under
the proxy while working standalone. The same build must work at both mounts:

1. **Assets: emit relative URLs.** Vite: `base: './'` in `vite.config.ts`.
2. **API/media calls: prefix with the detected mount.** Derive it once from the
   document URL and wrap every root-relative URL:

```ts
// '' standalone, '/apps/<id>' when embedded — same build works at both.
export const MOUNT: string = (() => {
  let p = window.location.pathname;
  if (!p.endsWith('/')) p = p.replace(/[^/]*$/, '');
  return p.replace(/\/+$/, '');
})();
export const withBase = (u: string): string =>
  u && u.charAt(0) === '/' ? MOUNT + u : u;

fetch(withBase('/api/things'));   // -> /apps/<id>/api/things when embedded
```

Everything else is handled by the proxy: your HTML is always revalidated (so a
rebuild shows up on reload), `/apps/<id>/api/*` reaches your same-origin API
with no manifest config, and a top-level visit to `/apps/<id>/` bounces the
user back into Ava's shell. One more tip from the field: gate your UI build on
a typecheck (`tsc -b --noEmit && vite build`) — bundlers don't catch unbound
identifiers, and a broken bundle inside an iframe is painful to debug.

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

### The tool facade — `ava-tools/1`

The contract *your app* implements to be discovered. Two routes; the Hub's
Detect finds them ahead of OpenAPI scraping, and the sample
(`examples/hello-app`) and note-keeper are conforming implementations.

```
GET /tools
-> 200 {
     "facade": "ava-tools/1",            // recommended — versions the contract
     "tools": [{
       "name": "list_personas",          // ^[a-z][a-z0-9_]{1,31}$
       "description": "…",               // <=200 chars — the agent reads this
       "inputSchema": {"type": "object", "properties": {…}, "required": […]},
       "access": "read"                  // read | write | destructive
     }]
   }

POST /call    {"name": "list_personas", "arguments": {…}}
-> 200 <the tool's JSON result>
-> 404 {"error": "unknown tool '…'"}     // name not in /tools
-> 400 {"error": "bad arguments: …"}     // input validation failed
-> 5xx {"error": "…"}                    // tool crashed — a message, never a blank 500
```

Rules of the road:

- **Curate.** Expose intent-level tools ("generate", "list_personas"), not your
  REST surface. Don't expose destructive ops you wouldn't hand an assistant —
  note-keeper exposes no deletes; deleting stays in the app's own UI.
- **`access` drives JIT consent** on Ava's side: `read` runs silently, `write`
  asks the operator on first use ("Always allow" is remembered), `destructive`
  asks every time and can never be always-allowed. Omitted -> `write` (safe).
  Tiers are self-reported: they can only make a tool *quieter*, never extend
  its reach — the egress policy, the operator's gate, and the audit ledger are
  Ava's, not the app's.
- **Results are for a model.** Return compact JSON; prefer ids and URLs over
  payloads; never inline binary/base64 blobs.
- **Auth** is optional: Ava sends `Authorization: Bearer <token>` when the
  manifest declares `token_env` on the discover block.
- `facade` is informational at version 1 — it exists so a future `ava-tools/2`
  can negotiate.

**Don't write it by hand — scaffold it.** In *your app's* repo:

```bash
ava app new myapp --framework fastapi   # or flask | express | stdlib
#   --port 9000   the port your app serves on
#   --ui          include the sidebar-tile ui: block (your app has a web UI)
```

writes `ava/surface.py` (a vendored, self-contained facade with a `tool()`
registry and the error contract built in — the file is yours, edit freely),
`ava/connector.yaml`, and `ava/README-AVA.md` with the wire-up for your
framework. Wire it in (one line), add your tools, then connect the app in
Ava's Hub — Detect finds the facade. `ava connector new` remains the Ava-side
scaffold; `ava app new` is the app-side one.

**Self-describe (optional): `GET /.well-known/ava.json`.** Detect tries this
first. It lets your app *tell* Ava what it is — a friendly name, where its
health check lives, whether it has an embeddable UI, and where the facade
routes are (so they don't have to sit at the root). All fields except `facade`
are optional; anything present prefills the Hub's connect form.

```json
{
  "facade": "ava-tools/1",
  "label": "Hello App",
  "tools": "/tools",              // path to the facade listing (default /tools)
  "call": "/call",                // path tools are invoked at (default /call)
  "health": "/api/health",        // prefills the health-probe field
  "ui": true                      // serves an embeddable web UI (sidebar tile)
}
```

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

### Agent skills (`SKILL.md`) — auto-surfaced in the Agent tab

A **skill** is a folder with a `SKILL.md` that coaches the model on *when and
how* to use its tools (progressive disclosure). Skills live in
`agent/skills/<id>/` (shipped) or `<overlay>/skills/<id>/` (private); every one
is deployed into the sandbox by `agent/install.sh` (`nemoclaw skill install`).

The filesystem is the single source of truth — **drop a folder and it appears**
in **Setup → Agent → Skills** with no registration. `ava_bridge/skills.py` globs
both locations and reads each SKILL.md's YAML frontmatter:

```yaml
---
name: ava-weather              # required
description: >                 # required — the model-facing trigger text
  How Ava answers weather questions via her get_weather tool. Use whenever…
title: Weather                 # optional — owner-facing card title (else derived from name)
summary: Live weather & forecasts.   # optional — one-liner (else the first sentence of description)
icon: cloud                    # optional — one of the app icon names
tools: [get_weather]           # optional — else inferred from the description
---
```

Only `name` + `description` are required; everything else is **derived
automatically** (title humanized from the name, summary cut from the
description, tools inferred). The card also shows a **deploy state** —
`live` / `edited · re-provision` / `not deployed · re-provision` — computed by
comparing the repo SKILL.md against the `data/skills_deployed.json` manifest that
`install.sh` writes, so a just-added skill honestly reads "re-provision" until
you run `ava agent provision`. A convention guard
(`tests/test_skill_frontmatter.py`) fails CI if a shipped SKILL.md lacks valid
frontmatter.

**Categories are owner-owned, never shipped.** Skills carry no category by
default — the product imposes no taxonomy, so every fork defines its own. Group
skills in the Agent → Skills panel via `ava.yaml` (per-instance, gitignored):

```yaml
skills:
  categories:                  # skill id/dir → your label
    ava-weather: Daily
    my-custom-skill: Work
```

With fewer than two categories defined the panel groups by **source** (Core
skills / Your skills) instead; add categories and it regroups by them. A guard
(`test_shipped_skills_declare_no_category`) keeps the shipped skills
taxonomy-free.

---

## 6. What's derived automatically

From that one manifest, with nothing hand-maintained in Ava's core:

- **Left-rail tile + embedded/native UI** ← `ui`
- **Ops dashboard health row** ← `service.probe`
- **Dashboard performance source** ← `perf`
- **Agent tools** ← `actions` (declared or discovered) or `mcp` (live from the server)
- **Agent skills panel** ← `agent/skills/*/SKILL.md` (drop a folder → it shows in
  Setup → Agent → Skills, with its deploy state)
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

### Optional-feature switches and guided-fix error codes

A service can name the `features.*` flag that governs it (`service.feature:
image`). When the user turns that feature off, the dashboard paints the service
as **off** (a neutral state), never as a red "down".

If your capability should be a user-facing switch, register it in
`ava_bridge/features.py` and gate its execution path with
`features.preflight(key, probe=...)`. That one registry entry gives you, with
no further wiring:

- a checkbox on **Setup → System → Optional features** (and the setup-save
  whitelist accepts its toggle),
- regular machine-readable error codes — `<key>_off` (switch off) and
  `<key>_down` (switch on, backing service unreachable) — which the chat UI
  turns into a guided **fix-it link** (hover explains where it leads; click
  deep-links to the right page). The frontend resolves fixes from the code
  *pattern* (`frontend/src/lib/fixes.ts`), so no frontend change is needed,
- a self-contained plain-text message ("Enable it under Setup → System →
  Optional features…"), so agent tools that simply relay `error` already tell
  the user what to do. Return coded errors from `/internal/*` routes as HTTP
  200 bodies (`{"error": ..., "error_code": ...}`) — the sandbox tool helper
  uses `curl --fail` and would swallow the body on a non-2xx status.

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
discovered (`GET /tools` + `POST /call` per the **`ava-tools/1` facade spec in
§5**, as here) or declared under `actions.static` in the manifest.

## 8. First-party vs third-party tiers

- **Third-party** (you): `embed: iframe` (your own UI) or `embed: none` (tools
  only), reached entirely through the generic proxy. This is the supported,
  no-core-edit path. The first-party apps were migrated onto exactly this path
  as the dogfood.
- **First-party native** (in-repo): a React view in `NATIVE_VIEWS`, plus
  optionally a dedicated proxy for media. Reserved for apps shipped inside
  Ava's bundle.

See also [PACKAGING_PLAN.md §5.3](PACKAGING_PLAN.md).
