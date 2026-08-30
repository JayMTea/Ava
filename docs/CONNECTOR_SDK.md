# Ava Connector SDK

Add your own app to Ava by dropping in a folder, with **no edits to Ava's
code**. A connector gives your app a left-rail tile, an embedded (or native) UI,
a health dot in the sidebar, a performance source, live agent tools, and
an auto-generated egress policy (an allow-list of the exact network destinations
Ava's agent may reach on your app's behalf) - all derived from one
`connector.yaml`.

This is the productization contract: fork Ava, connect **your** apps, ship.

!!! note "Are you on the right page?"

    **This page is for developers.** It is the complete reference for everything
    a `connector.yaml` can declare, and it is long on purpose.

    **If you just want to connect an app you already run**, you do not need any
    of it. Use the browser flow in
    [Connect your apps](CONNECT_YOUR_APPS.md) (Setup → Connectors → Connect an
    app), which detects the app and writes the manifest for you. Come back here
    when you want to change something it wrote, or when the app is one you are
    building yourself.

### What's on this page

| § | Section | Answers |
|---|---|---|
| 1 | [Where connectors live](#1-where-connectors-live) | Which folder do I drop it in, and how do I scaffold one? |
| 2 | [The manifest](#2-the-manifest) | Every field, annotated - and how credentials are named but never held |
| 3 | [The three embed tiers](#3-the-three-embed-tiers-how-your-ui-renders) | How does my UI render inside Ava, and what does embedding trust? |
| 4 | [Browser data-proxy](#4-browser-data-proxy-uiapi) | How does my browser UI call my own API without shipping a token to it? |
| 5 | [Agent tools](#5-agent-tools) | How do I give Ava tools - declared, discovered, or a real MCP server - and what gets an approval prompt? |
| 6 | [What's derived automatically](#6-whats-derived-automatically) | What do I get for free from that one file? |
| 7 | [Worked example](#7-worked-example-connect-the-sample-app) | Show me a complete runnable one |
| 8 | [First-party vs third-party](#8-first-party-vs-third-party-tiers) | Which path am I on? |

---

## 1. Where connectors live

Manifests are discovered from two roots (the second overrides the first by id):

1. **Built-in**: `<repo>/connectors/<id>/connector.yaml` (first-party, shipped)
2. **User**: `$AVA_HOME/connectors/<id>/connector.yaml` (yours)

`$AVA_HOME` defaults to the repo root; in Docker it is the mounted data volume.
Drop a folder in, restart Ava (or `ava up`), done. Folders starting with `_`/`.`
are ignored (that is why `_template/` is skipped).

### Scaffold and deploy one, end to end

The GUI writes a manifest for you; these five commands are the same thing by
hand. Each connector declares its health probe, metrics source, egress policy
and agent actions, and the dashboard, charts and agent tools all follow from it.

```bash
ava connector new mycrm                 # scaffold ${AVA_HOME:-$PWD}/connectors/mycrm/connector.yaml
$EDITOR "${AVA_HOME:-$PWD}/connectors/mycrm/connector.yaml"   # see the warning below
ava connector tools    mycrm --write    # generate the agent tools
ava connector policies mycrm --write    # generate its egress policy
(cd agent && ./install.sh)              # load them into the agent sandbox
```

```bash
ava connector list           # show all loaded connectors
ava connector apps           # show the left-rail app registry
```

(Or click **Deploy** on the connector's row in Setup → Connectors, which runs
the last three for you.)

!!! note "Don't skip the edit"

    The scaffold is the annotated reference template with every optional block
    commented out, so it declares a health probe and nothing else. Until you
    uncomment an `actions:` list or an `mcp:` block there is no agent surface to
    render, and both generate commands say so quietly rather than failing:

    ```
    $ ava connector tools mycrm --write

    0 tool(s) written — run `cd agent && ./install.sh` to deploy into the sandbox.
    $ echo $?
    0
    ```

    `./install.sh` also needs the agent runtime provisioned first. Without a
    NemoClaw CLI or sandbox it stops with the exact next command
    (`ava agent provision --install`, or `nemoclaw onboard`) rather than
    half-deploying.

    Deploying **one** app, once the kit is already in the sandbox:

    ```
    $ cd agent && ./install.sh --connector mycrm
    ```

    That applies `mycrm`'s egress policy and pushes the one server its tools live
    in, instead of re-pushing every MCP server, skill and policy to ship two
    generated files. `ava agent provision --connector mycrm` does the same
    through the bridge, and Setup → Connectors' **Deploy** button uses it
    automatically — falling back to a full deploy when the rest of the kit is not
    in the sandbox yet, because that is the run that puts it there.

---

## 2. The manifest

```yaml
id: mycrm                     # OPTIONAL — the FOLDER NAME is the id. Omit this,
                              #   or keep it equal to the folder name: a value
                              #   that disagrees is reported in Setup and
                              #   ignored. Ids match [a-z][a-z0-9_-]{1,31}.
label: My CRM
kind: app                     # core | inference | media | app
enabled: true                 # OPTIONAL, default true — set false to switch the
                              #   connector off WITHOUT deleting it. Management
                              #   UIs still list it so it can be turned back on;
                              #   everything else stops seeing it.
manifest_version: 1           # OPTIONAL — the schema this manifest was written
                              #   for. Declare a NEWER one than Ava understands
                              #   and it warns rather than failing, then ignores
                              #   the blocks it does not know.
base_url: "http://127.0.0.1:9000"   # OPTIONAL — the host that proxied action
                              #   calls go to; defaults to ui.url, then the
                              #   origin of service.probe

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
  # is recorded from its first call with no code changes. Keep the path
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
  # ...or, INSTEAD of static: bridge the app's own tool server (never both — see §5)
  # discover:
  #   base: "http://127.0.0.1:9000"
  #   list: "/tools"
  #   call: "/call"
  #   token_env: MYCRM_MCP_TOKEN

ingest:                       # OPTIONAL — let your app PUSH events to Ava rather
  enabled: true               #   than waiting to be asked. Get the inbound token
  channels:                   #   with `ava device token mycrm`, then POST to
    - { name: temperature, unit: "C" }   # /api/connectors/mycrm/events.
    - { name: motion, kind: event }      # `channels` is descriptive only.
                              # Full write-up: docs/DEVICE_CONNECTORS.md
```

`${VAR}` references expand from connector vars (`AVA_HOME`, `AVA_LOGS`,
`AVA_DATA`, `ROOT`) and then the process environment, so `${MYCRM_API_URL}`
resolves from Ava's config or env. `${NAME:-default}` falls back to `default`
when `NAME` is unset or empty, so one manifest can serve bare metal and Docker.

### Credentials: the manifest names the token, never holds it

Every auth field above (`auth.token_env`, `mcp.token_env`, `discover.token_env`,
`ui.api.token_env`, and any `${VAR}`) is the **name of an environment variable**,
not the secret. The value is resolved on the bridge, host-side, only when a
request is about to leave for your app - so the sandboxed agent never sees it,
and it is never written to the manifest. This is the hard **Ava-never-has-
passwords** invariant.

The value can come from two places, checked in this order:

1. **A real environment variable** (`export MYCRM_API_TOKEN=…`, systemd
   `EnvironmentFile=`, Docker `environment:`) - always wins.
2. **Ava's secret store** - paste the token once in **Setup → Connectors** (the
   *Access token / API key* field on connect, or *Add credential* on an existing
   row). It's saved `0600` under `$AVA_HOME/secrets/env/<NAME>`, survives restarts
   and every **redeploy** (you're never re-prompted), and is never placed in Ava's
   own environment, so no subprocess - the sandboxed agent included - inherits it.
   (One deliberate exception: a `${NAME}` you write into an `mcp.env` block is
   resolved and handed to that stdio server, because that is the whole point of
   declaring it.) Forkers don't have to touch `.env` at all - if you paste a value
   without naming a variable, Ava derives a stable one (`<CID>_TOKEN`).

Deploy/redeploy only regenerate tools + egress policy; they never read or ask for
a credential.

The one saved token does double duty: Ava sends it with your **agent tools** and,
for an `iframe` app, **presents it to the embedded UI** so the owner isn't asked
to log in to an app they already connected - see *Single sign-on* in §3.

---

## 3. The three embed tiers (how your UI renders)

Ava's left nav is **data-driven** from `/api/apps` (which reads the registry), so
your tile appears with no frontend edits. `ui.embed` picks how the shell renders
the app body:

| `embed` | Who it's for | How Ava renders it |
|---|---|---|
| **`iframe`** | **Third-party apps** (the common case) | Ava reverse-proxies your app's web UI **same-origin** under `/apps/<id>/` and shows it in an `<iframe>`. Same-origin means Ava's session cookie already gates the route, and if your app has its **own** login you make it seamless with one small step - see *Single sign-on* below. Ava's theme is passed as `?theme=light\|dark`. |
| **`native`** | First-party React views | Renders `NATIVE_VIEWS[view]`, provided by an optional gitignored overlay (`frontend/src/overlay/views/*`). Reserved for apps bundled into the frontend. |
| **`none`** | Apps with tools but no UI | Ava renders a generic **action console** listing the app's agent actions. |

### Why same-origin matters

Ava's session cookie is `httpOnly` + `SameSite=Lax` + host-only, so a raw
`http://127.0.0.1:9000` iframe would receive **no** cookie. Ava proxies your app
under its **own** origin (`/apps/<id>/`) so Ava's own cookie gates the route and
your app's own storage (a session cookie or a `localStorage` token) persists like
a normal same-origin visit. Precisely, the proxy's contract with your app is:

- **Cookies in**: the browser's cookies are forwarded to your app - except
  Ava's own session cookie, which is stripped and never reaches your app.
- **Cookies out**: your app's `Set-Cookie` is forwarded back to the browser
  with its `Path` rewritten into `/apps/<id>/` and any `Domain` attribute
  dropped, so the cookie stays scoped to your app's mount on Ava's origin.
- **Redirects**: an app-internal redirect (`Location: /login`) is rewritten
  into the mount (`/apps/<id>/login`) instead of escaping to Ava's own routes.
- **Methods**: `HEAD` and `OPTIONS` pass through to your app like any other
  method.
- **Streaming**: SSE and other streaming responses stream through unbuffered.

If your app has **no** auth of its own, you're done -
Ava's gate is the only door. If it **does** have a login, see the next section.

### iframe security

!!! warning "Embedding an app grants it your whole Ava session"

    The iframe is sandboxed (`allow-scripts allow-forms allow-same-origin
    allow-popups allow-downloads`), but `allow-scripts` + `allow-same-origin` is
    a documented no-op pairing: the frame keeps Ava's origin. Ava serves no CSP
    and `/api/hub/*` has no CSRF or `Origin` check, so an embedded app's
    JavaScript runs with your session and can call Ava's own API. It can also
    reach `parent.*`.

    Concretely, it can approve Ava's consent prompts on your behalf:

    ```js
    fetch('/api/hub/approvals').then(r => r.json())
      .then(j => j.pending.forEach(p => fetch('/api/hub/approvals/' + p.id, {method: 'POST'})));
    ```

    Embedding an app is closer to installing a browser extension than to opening
    a tab. **Review a third-party app before enabling it.** For an app whose
    bytes you do not control, `embed: none` - a tile plus Ava's generic action
    console, with no remote code in Ava's page - is the safer default.

An `Origin` check cannot close this: the proxy makes the frame *genuinely*
same-origin, so every request header is identical to the real SPA's. Isolating it
needs a second **origin**, which is what `apps.origin` does.

??? note "Isolating embedded apps on a second origin (`apps.origin`)"

    See the block in `config.example.yaml`. Two hostnames pointing at the same
    machine and port are two origins to a browser (separate cookie jars, no
    `parent` access), so it needs no second listener. With it set, `/apps/*` is
    served only on that host, everything else is refused there, and Ava hands
    the frame a short-lived per-connector token instead of a session
    (`/api/apps/<id>/embed` is what the shell asks for that URL; it answers
    `{"url": …, "isolated": true}`).

    **Set `server.trusted_hosts` at the same time, or every app tile breaks.**
    Ava's DNS-rebinding guard refuses any `Host` it doesn't recognise, and
    `apps.origin` is not added to that list automatically - so with
    `apps.origin` alone, `/apps/*` is refused on Ava's own host (`404 wrong
    origin`, by design) *and* the apps host itself answers `421 Misdirected
    Request`, leaving the app reachable at neither name. Both names must be
    declared:

    ```yaml
    apps:
      origin: "http://apps.ava.local:8096"   # [AVA_APPS_ORIGIN]
    server:
      trusted_hosts: ["apps.ava.local"]      # without this the frame 421s
    ```

    `apps.origin` is **unset by default** - turning it on requires you to make a
    second name resolve to this box - so until you do, the warning above is the
    security model.

### Make your UI mount-agnostic (the one requirement on your app)

Embedded, your app is served from `/apps/<id>/` instead of `/` - so a UI built
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
user back into Ava's shell. Cookies your app sets against a hardcoded `Path`
(`Path=/`, `Path=/login`) will appear to the browser scoped under `/apps/<id>/`,
because the proxy rewrites `Set-Cookie` paths into the mount like everything
else. One more tip from the field: gate your UI build on
a typecheck (`tsc -b --noEmit && vite build`) - bundlers don't catch unbound
identifiers, and a broken bundle inside an iframe is painful to debug.

### Single sign-on: apps with their own login

If your app has its **own** password/login, don't make the owner sign in again
after they've already connected it in Ava. The owner connects **once** (they
paste your app's token in Setup → Connectors, or it's auto-detected); Ava then
**presents that saved token on every embedded request** - the same value it uses
for your agent tools. Two small steps make your app honor it:

1. **Accept a static token as a full session.** Alongside your human login,
   treat a configured static token (referenced by your manifest's `token_env`,
   e.g. `auth.token_env: MYAPP_TOKEN`) as authenticated on every route your UI
   hits. Ava sends it as `Authorization: Bearer <token>` when the browser has no
   session of its own (a fresh embed). Media tags that can't set headers work
   too, because Ava injects on the proxied request, so a plain
   `withBase('/media/x')` is authenticated without a `?token=`.

2. **When embedded, skip your own login screen.** You already derive `MOUNT`
   (above); a non-empty `MOUNT` means "running inside Ava," where Ava's injected
   token authenticates every call. Start authenticated there and only fall back
   to your login on a real `401`:

```ts
export const EMBEDDED: boolean = MOUNT !== '';   // served under /apps/<id>
// ...
const [authed, setAuthed] = useState(() => !!getToken() || EMBEDDED);
// a 401 from any call still clears state and shows Login — the fallback for
// when Ava holds no token yet (the owner hasn't pasted one).
```

That's the whole contract. Standalone (direct at your port) your password login
is unchanged; embedded in Ava it's single sign-on. Ava resolves the token only
on the bridge when building the request - it never reaches the browser or the
sandboxed agent (the *Ava-never-has-passwords* invariant, §2).

!!! note "Self-describe it (optional, nicer onboarding)"

    Advertise the token name in `/.well-known/ava.json` as
    `"auth": {"token_env": "MYAPP_TOKEN"}` (§5). Ava's connect form then
    pre-fills the field so the owner just pastes the value.

---

## 4. Browser data-proxy (`ui.api`)

For an app whose UI (native or iframe) needs to call **its own** backend API from
the browser without exposing a token, declare `ui.api`. Ava forwards
`/apps/<id>/api/<path>` → `base + prefix + /<path>` with the `token_env` bearer
injected server-side (the browser never sees the token). `base` defaults to the
connector's own base: top-level `base_url`, else `ui.url`, else the origin of
`service.probe`.

!!! note "First-party note"

    An app that also streams **media** (arbitrary non-API paths) may keep a
    dedicated full reverse-proxy instead, for example a media app with its own
    `/<app>/*` proxy. The generic `/apps/<id>/api` proxy is API-only.

---

## 5. Agent tools

Two shapes, both reached through **one** generic bridge route
(`/internal/connector/<id>/<action>`), with no bespoke bridge code:

### Declared (`actions.static`)

Each becomes a generated tool calling your REST API through the proxy (up to 15
of them - see below):

```yaml
actions:
  static:
    - id: create_lead
      method: POST                 # GET args -> query params; POST -> JSON body
      path: "/api/leads/{owner}"   # {tmpl} path params are filled from args
      input: { owner: {type: string}, name: {type: string} }
```

`path` is appended to the connector's base (`base_url` in §2, else `ui.url`, else
the `service.probe` origin); a per-action `base:` overrides it.

Generate the `.mjs` tools: `ava connector tools mycrm --write`.

**Above 15 actions Ava switches to meta tools.** Declare 16 or more static
actions with a `path` (`META_TOOLS_MIN = 16`) and Ava stops generating one tool
per action: it emits two instead - `<id>_find_tool` (keyword-searches your action
set) and `<id>_call` (invokes one by name) - so a large REST surface can't flood
the agent's context on every turn. Same routes, same egress policy, same
approvals gate; only the tool shape changes. Declaring `actions.discover` or
`mcp:` alongside `actions.static` does the same swap but routes `<id>_call` to
the discover/MCP endpoint, leaving the declared actions unreachable - so don't
mix the two shapes in one manifest.

### Discovered (`actions.discover`)

Bridges an app that implements the **`ava-tools/1` HTTP facade** below: Ava GETs
`list` for the tool schemas and POSTs `call` `{name, arguments}` to invoke, so a
whole tool set is wired from one manifest with no per-tool declaration. This is
MCP-*shaped* but it is Ava's own protocol, **not MCP** - a server that speaks
real MCP (FastMCP, the official SDKs, `npx` servers) uses the `mcp:` block
instead (see *MCP servers* below). Reserved bridge actions `__tools` and
`__call` serve this.

### The tool facade - `ava-tools/1`

The contract *your app* implements to be discovered. Two routes; the Hub's
Detect finds them ahead of OpenAPI scraping. The shipped sample
(`examples/device-app`) is a conforming implementation.

```
GET /tools
-> 200 {
     "facade": "ava-tools/1",            // recommended — versions the contract
     "tools": [{
       "name": "list_personas",          // ^[a-z][a-z0-9_]{1,31}$
       "description": "…",               // <=200 chars — the agent reads this
       "inputSchema": {"type": "object", "properties": {…}, "required": […]},
       "access": "read"                  // read|sensitive|write|destructive|physical
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
  REST surface. Don't expose destructive ops you wouldn't hand an assistant: a
  good default is to expose no deletes at all and keep deleting in the app's
  own UI.
- **`access` drives JIT consent** on Ava's side: `read` runs silently,
  `sensitive`/`write` ask the operator on first use ("Always allow" is
  remembered), `destructive`/`physical` ask every time and can never be
  always-allowed. Omitted -> `write` (safe). Full table under *Access tiers*
  below. Tiers are self-reported: they can only make a tool *quieter*, never
  extend its reach - the egress policy, the operator's gate, and the audit
  ledger are Ava's, not the app's. And "quieter" is only honoured for a
  connector whose base is **loopback/private**: for a remote server Ava ignores
  the self-report and falls back to `write` unless the manifest opts in with
  `trust_declared_tiers: true`, because a remote tool list can change without
  the owner touching Ava.
- **Results are for a model.** Return compact JSON; prefer ids and URLs over
  payloads; never inline binary/base64 blobs.
- **Auth is required** unless the surface is loopback-only *and* you are certain
  nothing else on the host can reach it. Ava sends
  `Authorization: Bearer <token>` whenever the manifest declares `token_env` on
  the discover block, and the scaffolded surface **refuses every call without
  it**. This is the one place the trust boundary is genuinely yours: Ava's
  consent tiers gate what *Ava's agent* may invoke, and enforce nothing on your
  app's own port. An unauthenticated `/call` on an app that already listens on
  your LAN hands every registered tool - `destructive` included - to anyone who
  can send one `curl`. `/.well-known/ava.json` stays open so Detect works
  before the credential is in place.
- `facade` is informational at version 1 - it exists so a future `ava-tools/2`
  can negotiate.

**Don't write it by hand - scaffold it.** In *your app's* repo:

```bash
ava app new myapp --framework fastapi   # or flask | express | stdlib
#   --port 9000   the port your app serves on
#   --ui          include the sidebar-tile ui: block (your app has a web UI)
```

writes `ava/surface.py` (a vendored, self-contained facade with a `tool()`
registry and the error contract built in - the file is yours, edit freely),
`ava/connector.yaml`, and `ava/README-AVA.md` with the wire-up for your
framework. Wire it in (one line), add your tools, then connect the app in
Ava's Hub - Detect finds the facade. `ava connector new` remains the Ava-side
scaffold; `ava app new` is the app-side one.

**Self-describe (optional): `GET /.well-known/ava.json`.** Detect tries this
first. It lets your app *tell* Ava what it is - a friendly name, where its
health check lives, whether it has an embeddable UI, and where the facade
routes are (so they don't have to sit at the root). All fields except `facade`
are optional; anything present prefills the Hub's connect form.

```json
{
  "facade": "ava-tools/1",
  "label": "Hello App",
  "tools": "/tools",
  "call": "/call",
  "health": "/health",
  "ui": true
}
```

| Field | Meaning |
|---|---|
| `facade` | `"ava-tools/1"` - the only required field |
| `label` | friendly name, prefills the connect form |
| `tools` | path to the facade listing (default `/tools`) |
| `call` | path tools are invoked at (default `/call`) |
| `health` | prefills the health-probe field |
| `ui` | `true` if the app serves an embeddable web UI (sidebar tile) |

(Comments are shown as a table because the document your app serves must be
strict JSON - `//` comments would make Detect's parse fail.)

### MCP servers (`mcp:`): wrap any Model Context Protocol server

The headline path into the roughly 20,000-server MCP ecosystem. Point the
manifest at an MCP server and its tools are discovered and bridged live (Ava's
client speaks MCP revision 2025-03-26 over Streamable HTTP, stdio, and the
deprecated HTTP+SSE transport):

```yaml
mcp:
  url: "http://127.0.0.1:9200/mcp"     # Streamable HTTP transport
  token_env: MYAPP_TOKEN                # optional bearer
  # No `egress:` block is needed for the server itself — the agent never
  # reaches it (see the security model below); Ava allow-lists the two bridge
  # routes automatically. Consent tiers: Ava reads a top-level `access` on
  # each tools/list entry, else `_meta.access` / `_meta["ava/access"]`, else
  # the spec's ToolAnnotations (readOnlyHint -> read, destructiveHint ->
  # destructive); anything else falls to `dynamic_access` below.
  # transport: sse                      # deprecated HTTP+SSE (MCP 2024-11-05),
  #                                     # for servers that predate Streamable
  #                                     # HTTP — inferred when the url path
  #                                     # ends in /sse
  # or a stdio server (spawned by the bridge):
  # command: ["npx", "-y", "@modelcontextprotocol/server-github"]
  # env: { GITHUB_TOKEN: "${GITHUB_TOKEN}" }
  # sandbox: docker                     # run the stdio server contained (below)
  # image: node:24-slim                 # base image with the runtime — pin a
  #                                     # supported Node LTS; the built-in
  #                                     # default is still node:20-slim (EOL)
  # network: bridge                     # or `none` to cut the server off the net
```

**The security model: an egress policy around the agent, not around the
server.** Ava's
sandboxed agent never connects to the MCP server. The bridge speaks MCP
(JSON-RPC over Streamable HTTP or stdio) server-side, and the agent can reach
only the two policed bridge routes (`__tools`/`__call`) that the connector's
auto-generated egress policy allow-lists, and nothing else. A malicious or
compromised MCP server never gains a direct line into the sandbox, and the
sandbox never gains a direct line out.

!!! warning "A `command:` MCP server runs as you, on your host, unless you contain it"

    A `command:` server (for example `npx -y some-server`) is code you install.
    By default the bridge spawns it as your own user, with your files - the same
    trust model as every MCP desktop client, and the same broad power.

    Set **`sandbox: docker`** and Ava runs it inside a throwaway container
    instead: `--read-only`, a tmpfs for scratch, CPU/memory/pid caps,
    `no-new-privileges`, and **no host filesystem mounts**, so an untrusted
    server cannot touch your files. Add **`network: none`** to also cut it off
    the network.

    ```yaml
    mcp:
      command: ["npx", "-y", "@modelcontextprotocol/server-github"]
      sandbox: docker      # contain it
      network: none        # and cut its network
    ```

    Setup → Connect an app offers this as a one-click toggle, defaulted **on**
    when Docker is available.

**Per-action approval (`confirm`).** Gate a sensitive tool behind your explicit
OK: put `confirm: true` on a static action, `confirm: true` at the connector
level to gate every action, or `confirm: [action_id, …]` at the connector level to
gate just those. An author `confirm:` outranks the tier - it always asks and can
never be granted away. When the agent calls a gated action the call
**pauses**, an approval prompt appears with the arguments, and the action runs
only if you approve (or is refused on deny or timeout). Every request and
decision is written to the audit ledger.

!!! warning "The approvals ladder: what Ava will do without asking you"

    Every action carries a **tier**, and the tier decides whether Ava pauses and
    asks before running it. This is the consent gate for everything an app's
    tools can do. Set it explicitly with `access:` on a static action; otherwise
    it is inferred from the HTTP shape.

    | Tier | At call time |
    |---|---|
    | `read` | runs silently |
    | `sensitive` | no side effects, but **discloses** something (a chat corpus, a mailbox, a location history). Asks on first use; "Always allow" grants durably. Never inferred: it must be declared. |
    | `write` | asks on first use; "Always allow" grants durably |
    | `destructive` | asks every time - **never** grantable |
    | `physical` | **moves something in the real world** (relay, lock, valve). Asks every time - **never** grantable. Never inferred: it must be declared. |

    `destructive` and `physical` cannot be turned into "Always allow" by anyone,
    including you. That is the point of them.

Those five are the whole set (`ava_bridge/connectors.py` `_TIERS`); a value that
isn't one of them is an error row on Setup → Connectors, and the action falls
back to being inferred from its HTTP method. Inference only ever yields `read`
(GET/HEAD), `destructive` (DELETE, or a *delete* in the id/path) or `write` -
`sensitive` and `physical` exist precisely because no HTTP shape implies them.

Dynamic tools (MCP / discovered) have no static declaration to infer from, so
the manifest classifies them by name pattern - first match wins:

```yaml
dynamic_access:
  GetLiveContext: read        # fnmatch patterns
  "Vacuum*": write
  "*": physical
```

Classification order is: the manifest's `dynamic_access` patterns (the operator's
word - always wins) → the tier the app self-reported on its last `/tools`, **but
only for a loopback/private base** or with `trust_declared_tiers: true` (plain
MCP servers report none anyway, so those land on `write`) → otherwise `write`, or
`physical` on a `role: device` connector. A misspelled tier in `dynamic_access`
fails *closed* to `destructive` and shows an error row, rather than silently
falling through to `write`. **A `role: device` connector should
always declare a `"*": physical` catch-all**: the device fallback only applies to
tools Ava has never discovered, so once a tool has been seen the self-reported
`write` wins and "Always allow" becomes offerable. See the
[Home Assistant connector](CONNECT_HOME_ASSISTANT.md), which does exactly that.

!!! note "Migrating a facade to MCP? Keep the tiers"

    An `ava-tools/1` facade reports `access` per tool; plain MCP has no such
    field, so a hand-rolled port that drops it demotes every `read` to `write`
    and the owner starts getting prompted for things that used to run silently.
    Ava's MCP client (`ava_bridge/mcp_client.py`) looks for the tier in every
    place a real server can carry it, and lifts the first it finds to the
    top-level `access` the consent gate reads:

    1. a top-level `access` on the `tools/list` entry — never overridden;
    2. `_meta.access` or `_meta["ava/access"]` — the SDKs serialise `Tool.meta`
       as `_meta`, and it is the spec's home for vendor fields (what
       `sdk/host/ava_mcp` mirrors - see below);
    3. the spec's `ToolAnnotations`: `readOnlyHint: true` -> `read`,
       `destructiveHint: true` (and not read-only) -> `destructive`. Only an
       explicit `true` counts - `destructiveHint` defaults to true in the spec,
       so an omitted hint accuses nobody - and no hint ever yields `sensitive`
       or `physical`, which must be declared.

    A tool carrying none of those arrives with no tier at all and the manifest's
    `dynamic_access` decides (default `write`; `physical` on a `role: device`
    connector). So: emit `access` (top-level or under `_meta`) from your server,
    set the annotations, or restate the tiers here as `dynamic_access` patterns.

### Turn your own app into a real MCP server (`sdk/host/ava_mcp`)

The facade above is the quickest way in, but it is *Ava's* protocol - an app
that speaks only the facade is wired into Ava and nothing else. The SDK ships a
stdlib-only adapter that fronts it with genuine MCP, so the same tools answer
Ava and any MCP client that supports protocol revision 2025-03-26:

```bash
# ava_mcp is not an installed package — its PARENT dir must be on sys.path, so
# either point PYTHONPATH at sdk/host (below), `cd sdk/host` first, or
# `cp -r sdk/host/ava_mcp /path/to/your/app/` and run it from there.
PYTHONPATH=sdk/host python -m ava_mcp --facade http://127.0.0.1:8097 --port 9310 \
                  --token-env MYAPP_TOKEN --auth-env MYAPP_MCP_TOKEN
```

```yaml
mcp:
  url: "http://127.0.0.1:9310/mcp"
  token_env: MYAPP_MCP_TOKEN
```

No code changes in the app: run it as a sidecar next to your service. It
carries each tool's `access` tier through (top-level and mirrored under
`_meta`), so JIT consent behaves exactly as it did over the facade. A Python
app that would rather not run a second process can mount `RegistrySource`
in-process instead. Both credentials are named, never passed on argv:
`--token-env` is the *app's* token, `--auth-env` guards the MCP endpoint.

Full reference: [`sdk/host/ava_mcp/README.md`](../sdk/host/ava_mcp/README.md).

### Egress

`ava connector policies <id> --write` renders the connector's egress into
`agent/policies/generated/<id>.yaml`; `cd agent && ./install.sh` deploys it into
the sandbox. The generic proxy routes for your actions (and `__tools`/`__call`)
are allow-listed automatically.

!!! note "Where generated files live"

    Both generated trees — `policies/generated/` and
    `mcp_server_connectors/apps/` — hang off the **agent state root**, which is
    `$AVA_HOME/agent` (`paths.agent_state`, `AVA_AGENT_STATE_DIR`). On a plain
    checkout AVA_HOME *is* the repo, so that is literally the `agent/` directory
    beside this file and the paths above read as written.

    They are not source: they are rendered from your manifest, both are
    gitignored, and they are state your install has to keep. Under Docker the
    repo is an image layer, so keeping them there meant every `up --build` threw
    them away while the manifests that produced them survived on the volume —
    the connector came back in the list, silently un-deployed. `install.sh`
    reads the state root as well as the checkout and merges the two, so
    `ava connector … --write` followed by a provision works on either shape.

### Agent skills (`SKILL.md`) - auto-surfaced in the Agent tab

A **skill** is a folder with a `SKILL.md` that coaches the model on *when and
how* to use its tools (progressive disclosure). Skills live in
`agent/skills/<id>/` (shipped) or `<overlay>/skills/<id>/` (private); every one
is deployed into the sandbox by `agent/install.sh` (`nemoclaw skill install`).

The filesystem is the single source of truth - **drop a folder and it appears**
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
app: mycrm                     # optional — the connector id this skill drives, so
                               #   its card and tool chips carry that app's accent.
                               #   Needed for MCP/discover apps, whose tool names
                               #   carry no <id>_ prefix for Ava to infer from.
---
```

Only `name` + `description` are required; everything else is **derived
automatically** (title humanized from the name, summary cut from the
description, tools inferred). The card also shows a **deploy state** -
`live` / `edited · re-provision` / `not deployed · re-provision` - computed by
comparing the repo SKILL.md against the `$AVA_HOME/data/skills_deployed.json`
manifest that `install.sh` writes, so a just-added skill honestly reads
"re-provision" until you run `ava agent provision`. On a fresh fork that manifest
does not exist yet, so the state is *unknown* rather than a lie about being
deployed, and every card reads **`provision to load`**. A convention guard
(`tests/test_skill_frontmatter.py`) fails CI if a shipped SKILL.md lacks valid
frontmatter.

**Categories are owner-owned, never shipped.** Skills carry no category by
default - the product imposes no taxonomy, so every fork defines its own. Group
skills in the Agent → Skills panel via `ava.yaml` (per-instance, gitignored):

```yaml
skills:
  categories:                  # skill id/dir → your label
    ava-weather: Daily
    my-custom-skill: Work
```

With no categories defined the panel groups by **source** (Core skills / Your
skills) when you have both shipped and overlay skills, and renders a flat list
when you have only one source. The **first** category you create switches the
whole panel to category grouping, with anything uncategorised collected under
"General" (sorted last). A guard (`test_shipped_skills_declare_no_category`)
keeps the shipped skills taxonomy-free.

---

## 6. What's derived automatically

From that one manifest, with nothing hand-maintained in Ava's core:

- **Left-rail tile + embedded/native UI** ← `ui`
- **Sidebar health dot** ← `service.probe`
- **Dashboard performance source** ← `perf`
- **Agent tools** ← `actions` (declared or discovered) or `mcp` (live from the server)
- **Agent skills panel** ← `agent/skills/*/SKILL.md` (drop a folder → it shows in
  Setup → Agent → Skills, with its deploy state)
- **The persona's "connected apps" block** ← every enabled `kind: app` manifest
  that exposes tools, plus the tool names the app reported on its last
  discovery. `agent/render_persona.py` fills `{{APPS_BLOCK}}` at provision time
  with one line per app - its label, whether it is reached through native
  `<id>_<action>` tools or the `<id>_find_tool` → `<id>_call` pair, and up to
  25 tool names - so the model routes "what did I eat today" to the right app
  without guessing. Nothing about your apps lives in the tracked template;
  re-apply the persona (Setup → Agent, or `ava agent provision`) after
  connecting an app so the running agent learns it is there.
- **Agent egress policy** ← `egress` + `actions` + `mcp`
- **Browser data-proxy** ← `ui.api`
- **Chat quick-cards** ← `chat_pickup` (after a turn used one of your tools,
  Ava reads your app's log for artifacts produced during the turn and attaches
  them as deterministic cards; app-relative URLs resolve via `/apps/<id>`)
- **Ops job attribution** ← `jobs` (your app's active jobs appear next to the
  GPU graph so spikes are self-explanatory)
- **Loaded-model role labels** ← `model_hints` (label what a checkpoint is for).
  Ava's *own* roles no longer travel as this text: the hardware monitor emits a
  machine token `role_key` (`brain` / `""`) that
  the frontend words, because the backend returns facts and owner-facing copy
  lives in the SPA. `role_key: brain` is decided solely by
  `models.effective_brain()` - never by a model's name. Your hint still fills
  the row's free-text `role` field, which is where connector-declared copy
  belongs (the documented registry exception), for any model nothing built-in
  claims.

- **Model-row ownership** ← `owns` (say which running things on this machine are
  yours, so Ava's hardware monitor lists them under **Connected apps** instead of
  "other software"):

  ```yaml
  owns:
    container: [myapp-llm]                 # exact docker name(s)
    cmdline:   ["--served-model-name myapp", "/myapp/main.py"]
    paths:     ["/opt/myapp"]              # prefix of its weight files
  ```

  Any one match claims the row, tried in that order: container name, then a
  substring of the process command line, then a prefix of a mapped weight file.
  Substrings are lowercased and matched exactly as `model_hints` does.

  This is **declared, not inferred**, on purpose. Ava will not guess ownership by
  matching your `base_url` port against whatever a process is listening on: your
  manifest names the port *your API* answers on, while the engine you run
  usually sits on a different one, so the guess is wrong more often than right.
  An unclaimed GPU process is reported honestly as other software rather than
  attributed to the nearest plausible app.

The `chat_pickup` / `jobs` / `model_hints` / `owns` field shapes are documented
inline in
[`connectors/_template/connector.yaml`](../connectors/_template/connector.yaml).

### Optional-feature switches and guided-fix error codes

A service can name the `features.*` flag that governs it (`service.feature:
voice`). When the user turns that feature off, the dashboard paints the service
as **off** (a neutral state), never as a red "down".

If your capability should be a user-facing switch, register it in
`ava_bridge/features.py` and gate its execution path with
`features.preflight(key, probe=...)`. That one registry entry gives you, with
no further wiring:

- a checkbox on **Setup → System → Optional features** (and the setup-save
  whitelist accepts its toggle),
- regular machine-readable error codes - `<key>_off` (switch off) and
  `<key>_down` (switch on, backing service unreachable) - which the chat UI
  turns into a guided **fix-it link** (hover explains where it leads; click
  deep-links to the right page). The frontend resolves fixes from the code
  *pattern* (`frontend/src/lib/fixes.ts`), so no frontend change is needed,
- a self-contained plain-text message ("Enable it under Setup → System →
  Optional features…"), so agent tools that simply relay `error` already tell
  the user what to do. Return coded errors from `/internal/*` routes as HTTP
  200 bodies (`{"error": ..., "error_code": ...}`) - the sandbox tool helper
  uses `curl --fail` and would swallow the body on a non-2xx status.

---

## 7. Worked example: connect the sample app

[`examples/device-app/`](../examples/device-app/) is a complete, runnable
third-party connector (discovered tools, a health probe, and a push channel) in
about 150 lines. It is the acceptance test for "fork Ava, add your app, zero
source edits":

```bash
# 1. Start the app's own web server (its /health + /tools + /call)
python3 examples/device-app/server.py       # serves http://127.0.0.1:8479

# 2. Register it with Ava by dropping the folder into your data root
mkdir -p "${AVA_HOME:-$PWD}/connectors"          # ava setup does not create this
cp -r examples/device-app "${AVA_HOME:-$PWD}/connectors/device-demo"

# 3. Generate its agent tools + egress policy and load them into the sandbox.
#    The first two write into the repo and need nothing installed. install.sh
#    DEPLOYS into a NemoClaw sandbox, so it needs the agent runtime provisioned
#    first (`ava agent provision --install`); without it it stops with
#    "nemoclaw CLI not found" / "sandbox not found" and changes nothing.
ava connector tools    device-demo --write
ava connector policies device-demo --write
(cd agent && ./install.sh)

# 4. Restart Ava (or `ava up`) and open the web app
#    -> "Device Demo" appears under Devices
#    -> ask Ava: "what is the demo temperature?"  (its tools were discovered live)
```

(Or skip step 3 entirely: **Deploy** on the app's row in Setup → Connectors runs
all of it.)

| File | Role |
|---|---|
| `connector.yaml` | The manifest. `role: device` groups it under Devices. `service.probe` gives the dashboard health row. `actions.discover` bridges the app's tool set. `ingest` opens the push channel. `egress` renders into the agent's network policy. |
| `server.py` | The app: a stdlib-only HTTP server exposing `/health`, `/tools`, `/call`, plus a `push_event` helper. Replace it with your real app. |

The contract the app implements: `GET /health` returns `{"ok": true}`; tools are
either discovered (`GET /tools` + `POST /call` per the **`ava-tools/1` facade
spec in §5**, as here) or declared under `actions.static` in the manifest. To
also embed your app's own web page as a tile in the left rail, add a `ui:` block
with `embed: iframe` and its `url` - see §2.

## 8. First-party vs third-party tiers

- **Third-party** (you): `embed: iframe` (your own UI) or `embed: none` (tools
  only), reached entirely through the generic proxy. This is the supported,
  no-core-edit path. The first-party apps were migrated onto exactly this path
  as the dogfood.
- **First-party native** (in-repo): a React view in `NATIVE_VIEWS`, plus
  optionally a dedicated proxy for media. Reserved for apps shipped inside
  Ava's bundle.
