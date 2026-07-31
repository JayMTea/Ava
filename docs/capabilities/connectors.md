# Apps, devices & MCP

Connecting an app to Ava is one YAML file. That file — the connector
manifest — is the whole integration: it wires the app into the dashboards,
generates the agent's tools for it, and renders the security policy that
decides what the agent may reach. Nothing in Ava's core changes, which is the
point: a fork that adds three of your own apps still has zero source edits.

This page is the *what you get* and *what stops it*. The field-by-field
manifest reference lives in the [Connector SDK](../CONNECTOR_SDK.md).

## What ships

Four connectors are built in and enabled on a fresh install. All four are
plumbing — Ava's own moving parts, reported so the dashboards can say
"healthy" honestly.

| Connector | What it contributes | Agent tools |
|---|---|:--:|
| `bridge` | Health row for Ava's own web app, plus the `ava` perf source every Vitals chart reads. | — |
| `local-llm` | Health probe for the OpenAI-compatible inference server. | — |
| `router` | Health probe for the inference router (`/healthz`). | — |
| `gpu-service` | Health row for the image engine, gated on `features.image` so "off" never paints red. Its egress block allow-lists `POST /internal/run-gpu-job`. | — |

All four expose **no agent surface at all** — they are health and metrics
rows, and a fresh install has **zero connector-generated agent tools**. Image
generation does reach the agent, but through a first-party tool calling
`/internal/run-gpu-job`, not a generated connector tool: the `gpu-service`
action declares no `path`, so nothing is generated for it.

That emptiness is the design. The apps worth connecting are *yours*, so Ava
ships the machinery and none of the opinions. Adding your first app is
[Setup → Connectors → Connect an app](../CONNECT_YOUR_APPS.md), or one file
copied into `$AVA_HOME/connectors/`.

## Worked examples you can copy in

Six complete manifests ship under [`examples/`](../../examples/).
Each is a folder you copy into your data root — no edits to Ava's core:

| Example | Copy it in with | What it demonstrates |
|---|---|---|
| `hello-app` | `cp -r examples/hello-app "$AVA_HOME/connectors/hello"` | The whole loop end to end: health row, left-rail iframe tab, and a live-discovered tool set. |
| `device-app` | `cp -r examples/device-app "$AVA_HOME/connectors/device-demo"` | `role: device` plus **push** ingest — the app hands Ava sensor events when *it* decides. |
| `home-assistant` | `cp -r examples/home-assistant "$AVA_HOME/connectors/home-assistant"` | A real MCP integration over the legacy HTTP+SSE transport, with every actuating tool pinned to the `physical` tier. |
| `stridewell` | `cp -r examples/stridewell "$AVA_HOME/connectors/stridewell"` | A health app over **real MCP**. Its reads are `sensitive`, not `read` — the tier answers what a disclosure costs, not whether the call mutates. |
| `ledgerline` | `cp -r examples/ledgerline "$AVA_HOME/connectors/ledgerline"` | Personal finance, **read-only by design**, with `confirm:` on the one tool that produces a portable file. |
| `hearthwire` | `cp -r examples/hearthwire "$AVA_HOME/connectors/hearthwire"` | Home control — the only example that declares `physical`, the tier Ava will never infer for you, plus `confirm:` on the door lock. |

Two of them are worth reading as opposite ends of the model.

`hello-app` and `device-app` **bridge a tool set live**: `actions.discover`
points at the app's own `/tools` and `/call`, so Ava fetches the schemas at
load time and every tool the app grows appears with zero per-tool wiring. The
alternative is declaring `actions:` statically, one entry per endpoint with an
explicit `access:` tier — the reference for that shape is
[`connectors/_template/connector.yaml`](../../connectors/_template/connector.yaml),
which annotates every field.

`home-assistant` is fully dynamic — the entire Home Assistant integration is
that one manifest, and it ships **inert until configured**: an unset
`${HASS_URL}` leaves it with nothing to talk to, so copying it in before you
have Home Assistant costs you nothing.

## What one manifest gives you

Every surface below is derived from the manifest at load time. None of it is
hand-maintained anywhere in Ava's core.

- **A health row** on [Operations](operations.md) → Service health, from
  `service.probe`. Name the `features.*` flag that governs the service
  (`service.feature: image`) and a dead probe reads *off* rather than *down*.
- **A perf source** charted on [Vitals](vitals.md), from `perf.path` — or, if
  your app never writes a `performance.jsonl`, from the bridge itself: every
  proxied connector call is timed and written to
  `${AVA_LOGS}/apps/<id>/performance.jsonl` with its latency and HTTP status.
  That file registers itself, so a brand-new app appears in Vitals on its
  first call with no restart.
- **A dashboard tile** in the Connected apps grid, with per-app call count and
  energy attribution.
- **A same-origin tab in the sidebar**, from a `ui:` block with
  `embed: iframe`. Ava reverse-proxies the app under `/apps/<id>/` so it
  inherits your session cookie — no second login, no third-party cookies.
- **Agent tools**, generated as `.mjs` into the sandbox
  (`ava connector tools <id> --write`, or **Deploy** in the browser).
- **An egress policy**, rendered into the same shape as
  `agent/policies/*.yaml` and namespaced `ava-<id>`. It allow-lists the
  specific bridge routes this connector's tools use, and nothing else.

Also derived, and documented in the [SDK](../CONNECTOR_SDK.md): chat quick-cards
(`chat_pickup`), job attribution next to the GPU graph (`jobs`), loaded-model
role labels (`model_hints`), and a browser data-proxy that injects the app's
bearer token server-side (`ui.api`) so the browser never sees it.

## Three transports

`transport` is resolved from the manifest, and it is the honest name for the
wire protocol — not a badge that says "MCP" for anything with tools.

| Transport | Manifest | What it is |
|---|---|---|
| `mcp` | `mcp:` | A real Model Context Protocol server. Ava speaks JSON-RPC 2.0 to it over Streamable HTTP, the legacy HTTP+SSE transport (what Home Assistant's MCP Server integration speaks), or stdio against a spawned subprocess. |
| `discover` | `actions.discover` | Ava's own `ava-tools/1` HTTP facade — MCP-*shaped*, but not MCP. Ava GETs your tool list and POSTs `{name, arguments}` to call. |
| `rest` | `actions:` | Statically declared actions proxied to the app's own REST API, with `{tmpl}` path params filled from the call arguments. |

A connector with none of those reports `none`: a UI-only app, or a push-only
device.

## The meta-tool switch

Past roughly fifteen tools, per-action schemas bloat the agent's context on
every single turn and tool selection degrades. So Ava collapses large tool
sets to **exactly two generated tools**:

```
<id>_find_tool   keyword-search this connector's actions
<id>_call        invoke one by name
```

This happens for every dynamic connector (`mcp:` or `actions.discover` — their
tool set is not knowable at generation time), and for any static connector
declaring **16 or more** actions with a `path` (`META_TOOLS_MIN = 16`). Below
that threshold you get one tool per action — so a static app declaring 15
endpoints gives the agent 15 named tools, while the same app at 16 collapses to
the two meta-tools, and every `mcp:`/`discover` app gets the pair regardless of
size.

The search and the cap run **server-side**, on the bridge: `find_tool` filters
by keyword, ranks by match count, truncates to the requested limit, and
reports the pre-cap total so the agent knows to refine. The full schema list
never enters the sandbox.

Same routes, same egress policy, same approvals gate — only the tool shape
changes. Full detail, including what happens if you mix static and dynamic
shapes in one manifest, is in the [Connector SDK](../CONNECTOR_SDK.md).

## Permissions: four tiers, decided at call time

Connecting an app never shows you an endpoint review you cannot evaluate.
Consent is just-in-time instead, and the tier of the action decides how it is
asked.

| Tier | Behaviour |
|---|---|
| `read` | Runs silently. |
| `write` | Asks on **first** use. Answering "Always allow" silences it from then on. |
| `destructive` | Asks **every** time. Never grantable. |
| `physical` | Asks **every** time. Never grantable. |

An author's explicit `confirm: true` — on the connector or on one action —
always asks and can never be granted away, whatever the tier.

For a static action the tier is explicit `access:` if declared, else inferred:
`GET`/`HEAD` → `read`; `DELETE`, or "delete" in the action id or path →
`destructive`; everything else → `write`. **`physical` is never inferred.** It
moves something in the real world — a relay, a lock, a valve — so it has to be
declared. The code's own justification for making it ungrantable is that a
lock must not become a one-tap-then-silent action.

!!! warning "The one exception — and it is the most security-relevant default in the model"

    A *dynamically discovered* tool has no static declaration to infer from.
    It is classified by the manifest's `dynamic_access` fnmatch patterns
    (first match wins), then by the app's own self-reported tier, and if
    neither answers it falls back to **`physical` on a `role: device`
    connector** and `write` everywhere else.

    A device's unknown verbs are presumed to move something until the author
    says otherwise. That is why `home-assistant` — whose tool set is whatever
    you exposed to Assist — reads live state silently but asks every single
    time before it turns anything on, with no way to grant that away.

The operator's manifest always outranks the app's self-report. An app can
declare one of its tools `read`; a `dynamic_access` pattern saying `physical`
overrides it.

An approval **blocks the calling request** on a condition variable until you
answer, for up to 120 seconds. More than 50 parked calls fails closed to
`denied`. Both the request and the outcome are audit events.

### Grants

Answering "Always allow" writes a durable grant to
`$AVA_HOME/connector_grants.yaml`:

```yaml
persona:
  post_persona: {granted: "2026-07-08T20:41:00Z", by: owner}
```

Revoking is deleting the entry — from **Setup → Connectors → Permissions**, or
the API. Grant and revoke are **both** audit events, so "what can this app do,
and since when" is always answerable from the ledger rather than from memory.

That file is also on the agent's hard-deny list: the self-editing access
policy refuses to write `connector_grants.yaml` at all, alongside `ava.yaml`,
`secrets/**` and `.git/**`. The reasoning is in the source comment — the
grants file is the connector consent ledger, so a writable one is
self-approval. Ava cannot grant herself a permission you did not give her.
See [the agent page](agent.md) for the rest of that policy.

## The trust boundary

This is the part worth being precise about.

**Ava's sandboxed agent never talks to a connector's API, and never talks to
an MCP server.** Not to a local one, not to a remote one. The MCP client runs
host-side, in the bridge. A stdio MCP server runs as a host process you
declared in the manifest — the same trust model as any MCP desktop client —
but the agent's blast radius does not include it.

What the agent can reach is one generic bridge route:

```
/internal/connector/<id>/<action>
```

allow-listed per action in that connector's generated egress policy. For MCP,
tool-facade and meta-tool connectors, that narrows to exactly two paths:

```
GET  /internal/connector/<id>/__tools     list (filtered, capped)
POST /internal/connector/<id>/__call      invoke by name
```

For an MCP connector, **those two routes are the entire agent-side surface.**
The sandbox reaches the bridge over `host.openshell.internal:8096` holding a
capability-scoped internal token, and the policy engine enforces the route
list; everything else is denied by the sandbox's network policy.

Every call through that route is recorded:

- an `egress` audit event with the connector, the tool, and the outcome — the
  HTTP status when it ran, or `blocked:denied` / `blocked:timeout` when the
  approvals gate refused it. Refused calls are on the record too, which is the
  half most systems drop.
- latency and status into that app's perf log, which is how a connected app
  shows up on Vitals without writing a line of telemetry itself.

Read the ledger under **Setup → History**, or export it from
[Data](data.md) → Maintenance.

### What contains the server, per transport

The paragraphs above are about the **agent**, and they hold for every MCP
transport. What is contained on the *server* side differs, and it is worth
stating plainly rather than leaving one sentence to cover three cases:

| `transport` | What constrains the server |
|---|---|
| `http` / `sse` | **Nothing.** The bridge posts to the URL from your manifest. There is no host allow-list and no SSRF guard on this path — unlike Ava's web-search fetch, which re-validates every redirect hop. A remote MCP server is as trusted as the operator who declared it. |
| `stdio` + `sandbox: docker` | A throwaway container: read-only rootfs, tmpfs `/tmp` and `/root`, `--cpus 1`, `--memory 512m`, `--pids-limit 256`, `no-new-privileges`, no host mounts, and only the `env:` your manifest declares. Network defaults to `bridge` (outbound is open) — set `network: none` to cut it. |
| `stdio` (default) | A host process running as the bridge user, with your filesystem and your network. The one mitigation is that Ava passes it a **stripped environment** rather than `os.environ`, so it does not inherit `ANTHROPIC_API_KEY` or your connector credentials — anything it legitimately needs you declare in `env:`. |

So `sandbox: docker` is the setting that makes "contained" true of the server as
well as the agent. Ava negotiates MCP revision `2025-03-26`
(`ava_bridge/mcp_client.py`).

## Devices

A connector is a device if it declares `role: device` **or** an `ingest:`
block. `role: device` groups it under Devices in the UI and switches the
dynamic-tool fallback to `physical`; `ingest:` opens the push channel. They
are independent, and both are additive on top of the normal pull path — a
device's `mcp:` or `actions:` block works exactly as any app's does.

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Screen recording: connecting a device and watching its first pushed reading arrive">
  <source src="../../assets/connect-device-tour.mp4" type="video/mp4">
  Your browser can't play video. <a href="../../assets/connect-device-tour.mp4">Download the walkthrough</a>.
</video>

### Push: your app hands Ava an event

Pull is Ava asking. Push is your app deciding — "motion detected", "tank at
12%" — and handing Ava the event. The device logic and the decision to notify
both stay in your app.

Authentication is a **per-connector bearer token**, derived from Ava's root
internal token:

```
HMAC-SHA256(internal_token, "ava-ingest:<id>")
```

That is a deliberately **separate namespace** from the `ava-internal:<group>`
tokens Ava's own sandboxed MCP servers hold. An app that can push events
cannot reach `/internal/*` — it holds a token that authorizes exactly one
thing. There is no new secret store, and changing the connector id rotates
its token.

Read yours with `ava device token <id>`, or copy it from the ⋯ menu on
**Setup → Connectors** (*Push token*).

```bash
curl -X POST http://127.0.0.1:8096/api/connectors/greenhouse/events \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"type":"reading","name":"soil_moisture","value":18,"unit":"%",
       "severity":"warn","notify":true,"message":"Bed 3 is dry"}'
```

The guards run in this order, and each has its own status code:

1. **Bearer token** — wrong or missing → `401`.
2. **`ingest.enabled`** in the manifest → `404` if the connector never opted in.
3. **Token bucket**, per connector — 600 events/min, burst 60 → `429`.
4. **Body cap**, 64 KiB — a single event is tiny → `413`.
5. **JSON parse** → `400`.
6. **Field normalization** → `400` with the reason.

Accepted fields:

| Field | Notes |
|---|---|
| `type` | `event` or `reading`. Defaults to `event`; anything else is rejected. |
| `name` | **Required.** Capped at 64 characters. |
| `ts` | Unix seconds. Defaults to now. |
| `value` | Coerced to float where possible, else kept as a capped string. |
| `unit` | Capped at 16 characters. |
| `message` | Capped at 500 characters. |
| `severity` | `info`, `warn` or `critical`. Anything else is dropped. |
| `notify` | Boolean. |

Every accepted event does three things: it is appended to
`${AVA_LOGS}/devices/<id>.jsonl` under an advisory lock, with the same bounded
rotation the perf log uses — best-effort, so a full disk never fails your
app's push; it enters a 500-entry in-process ring with a monotonic sequence
number, which feeds the `device.event` frame on the `/api/stream/ops` SSE
stream so it appears on Operations live; and if it is `notify`, `warn` or
`critical`, it is raised as an alert. Operations has a per-browser **Speak
alerts** toggle that reads those aloud.

Read events back from Operations → Device events, `GET /api/devices`,
`ava device events <id>`, or the agent's own `device_events` tool.

The full walkthrough, including a runnable example app in about 150 lines, is
in [Device connectors](../DEVICE_CONNECTORS.md). For Home Assistant
specifically, see [Connect your Home Assistant](../CONNECT_HOME_ASSISTANT.md).

## Manifest robustness

A connector manifest is a file *you* wrote, so the loader treats it as such.

- **Per-block quarantine, never per-connector.** If a block is the wrong type
  — `egress:` as a string instead of a mapping — that block is dropped from
  the **in-memory** copy and reported, and the rest of the connector loads.
  Your file is never rewritten. The reason is concrete: the one screen that
  can fix a broken manifest is Setup → Connectors, which is also the screen a
  crash on `egress:` would take down.
- **Unknown top-level keys warn**, rather than silently doing nothing — that
  is how `egres:` goes unnoticed forever. Prefix your own keys with `x_` to
  silence the warning.
- **A newer `manifest_version` still loads**, with a warning that unknown
  blocks are ignored. Forward-compatibility by ignoring what it does not
  understand is what keeps a fork's manifests portable.
- **A malformed YAML file never crashes boot.** It lands in the load-error
  list, which is surfaced at the top of Setup → Connectors and in
  `ava doctor`, so a bad manifest is visible rather than absent.

## Connecting one from the browser

No files and no terminal: **Setup → Connectors → Connect an app**.

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Screen recording: connecting an app from the Setup hub, end to end">
  <source src="../../assets/connect-app-tour.mp4" type="video/mp4">
  Your browser can't play video. <a href="../../assets/connect-app-tour.mp4">Download the walkthrough</a>.
</video>

Name the app, then paste **where it is** — a web address like
`http://127.0.0.1:9000`, or a start command like
`npx -y @modelcontextprotocol/server-filesystem ~/notes` — and click
**Detect**. Ava probes it in order: a self-describing `/.well-known/ava.json`,
then MCP over Streamable HTTP, then the `ava-tools/1` facade at `/tools`, then
a published OpenAPI spec (`/openapi.json`, `/swagger.json`, `/openapi`) whose
paths it turns into a pre-filled action list. If nothing is discoverable, the
form lets you declare the actions by hand. A start-command server can be run
inside an isolated container from the same form.

![The Connectors list after connecting: the new app at the top, enabled, with its action count and deploy state](../assets/connect-app-3-connected.png)

Connectors group by identity, not protocol — **Devices**, **Apps**, **Tools**
— because a device that speaks MCP is still a device. Each row carries its
transport badge (MCP / tool facade / REST), its action count, its credential
state, and a deploy indicator that appears only when the generated tools or
egress policy have drifted from the manifest. From the row you get:

- **Permissions** — every action grouped by capability, with its tier and its
  grant state. This is where you revoke an "Always allow".
- **Preview** — the exact tools and egress policy the manifest renders, without
  touching the agent.
- **Deploy** / **Redeploy** — regenerate both into the sandbox.
- **⋯** — push token, credential, **Appearance** (icon and accent colour),
  an inline **manifest editor**, disable, and remove.

Credentials never enter the manifest. It stores only the *name* of an
environment variable; if you paste a value, it is saved once to Ava's
server-side secret store and presented to the agent tools and the embedded UI
from there.

The step-by-step version of this flow, with screenshots, is
[Connect your apps](../CONNECT_YOUR_APPS.md).

## Where to go next

- **[Connect your apps](../CONNECT_YOUR_APPS.md)** — the guided no-code walkthrough.
- **[Connector SDK](../CONNECTOR_SDK.md)** — the full manifest reference, the
  `ava-tools/1` facade contract, embed tiers, and a runnable example.
- **[Device connectors](../DEVICE_CONNECTORS.md)** — the pull and push paths for
  your own hardware.
- **[Connect your Home Assistant](../CONNECT_HOME_ASSISTANT.md)** — one manifest,
  two environment variables.
- **[The agent](agent.md)** — the sandbox on the other side of the boundary,
  and the capability-scoped tokens that police it.
- **[Security](../../SECURITY.md)** — the trust model end to end.
