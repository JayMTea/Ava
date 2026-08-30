# Apps, devices & MCP

A **connector** is how you tell Ava about one of your apps. It is a single YAML
file, and that file is the whole integration: it wires the app into the
dashboards, generates the tools the agent can call, and renders the security
policy that decides what the agent may reach. Nothing in Ava's core changes,
which is the point. A fork that adds three of your own apps still has zero
source edits.

A connected app gets its own place in the sidebar, its own health row, and its
own tools that Ava can call in chat.

!!! note "MCP, in one sentence"

    **MCP** (Model Context Protocol) is an open standard for describing the
    tools an app offers, so any assistant can discover and call them without
    bespoke code. Ava speaks it, but it is one of three ways to connect an app,
    not a requirement.

## Which page does what

This page is the **concept**: what a connector is, what it gives you, and what
stops it. The step-by-step lives elsewhere.

| Page | What it is for |
|---|---|
| This page | What a connector is, what one manifest gives you, and the permission and trust model |
| [Connect your apps](../CONNECT_YOUR_APPS.md) | The guided, no-code walkthrough. Get-started step 4 |
| [Connector SDK](../CONNECTOR_SDK.md) | The field-by-field manifest reference and the `ava-tools/1` facade contract |
| [Device connectors](../DEVICE_CONNECTORS.md) | Wiring your own hardware, pull and push, with a runnable example app |
| [Connect your Home Assistant](../CONNECT_HOME_ASSISTANT.md) | One manifest, two environment variables |

## What one manifest gives you

Every surface below is derived from the manifest at load time. None of it is
hand-maintained anywhere in Ava's core.

- **A health dot** beside the app's tab in the sidebar, from `service.probe`.
  Name the `features.*` flag that governs the service (`service.feature: voice`)
  and a dead probe reads *off* rather than *down*.
- **A perf source**, from `perf.path`. If your app never writes a
  `performance.jsonl`, the bridge writes one for it: every proxied connector
  call is timed into `${AVA_LOGS}/apps/<id>/performance.jsonl` with its latency
  and HTTP status, and that file registers itself, so a brand-new app is
  recorded on its first call with no restart. The agent reads them back with
  its `read_performance` tool.
- **A same-origin tab in the sidebar**, from a `ui:` block with `embed: iframe`.
  Ava reverse-proxies the app under `/apps/<id>/` so it inherits your session
  cookie. No second login, no third-party cookies.
- **Agent tools**, generated as `.mjs` into the sandbox
  (`ava connector tools <id> --write`, or **Deploy** in the browser).
- **An egress policy** (the allow-list of exactly which addresses the agent's
  sandbox may reach; anything not on it is refused), rendered into the same
  shape as `agent/policies/*.yaml` and namespaced `ava-<id>`. It allow-lists the
  specific bridge routes this connector's tools use, and nothing else.

Also derived, and documented in the [SDK](../CONNECTOR_SDK.md): chat quick-cards
(`chat_pickup`), job attribution next to the GPU graph (`jobs`), loaded-model
role labels (`model_hints`), and a browser data-proxy that injects the app's
bearer token server-side (`ui.api`) so the browser never sees it.

??? note "What ships built in, and why it is almost nothing"

    Three connectors are built in and enabled on a fresh install. All three are
    plumbing, which is to say Ava's own moving parts, reported so the dashboards
    can say "healthy" honestly.

    | Connector | What it contributes | Agent tools |
    |---|---|:--:|
    | `bridge` | Health probe for Ava's own web app, plus the `ava` perf source. | none |
    | `local-llm` | Health probe for the OpenAI-compatible inference server. | none |
    | `router` | Health probe for the inference router (`/healthz`). | none |

    All three expose **no agent surface at all**. They are health and metrics
    rows, and a fresh install has **zero connector-generated agent tools**.

    That emptiness is the design. The apps worth connecting are *yours*, so Ava
    ships the machinery and none of the opinions.

??? note "Three worked examples you can copy in"

    Three complete manifests ship under [`examples/`](../../examples/). Each is a
    folder you copy into your data root, with no edits to Ava's core:

    | Example | Copy it in with | What it demonstrates |
    |---|---|---|
    | `device-app` | `cp -r examples/device-app "${AVA_HOME:-$PWD}/connectors/device-demo"` | The whole loop end to end - health row, a live-discovered tool set - plus `role: device` and **push** ingest, so the app hands Ava sensor events when *it* decides. |
    | `home-assistant` | `cp -r examples/home-assistant "${AVA_HOME:-$PWD}/connectors/home-assistant"` | A real MCP integration over the legacy HTTP+SSE transport, with every actuating tool pinned to the `physical` tier - the one Ava will never infer for you. |

    The two are worth reading as opposite ends of the model.

    `device-app` **bridges a tool set live**: `actions.discover`
    points at the app's own `/tools` and `/call`, so Ava fetches the schemas at
    load time and every tool the app grows appears with zero per-tool wiring.
    The alternative is declaring `actions:` statically, one entry per endpoint
    with an explicit `access:` tier. The reference for that shape is
    [`connectors/_template/connector.yaml`](../../connectors/_template/connector.yaml),
    which annotates every field.

    `home-assistant` is fully dynamic. The entire Home Assistant integration is
    that one manifest, and it ships **inert until configured**: an unset
    `${HASS_URL}` leaves it with nothing to talk to, so copying it in before you
    have Home Assistant costs you nothing.

## Three transports

`transport` is resolved from the manifest, and it is the honest name for the
wire protocol, not a badge that says "MCP" for anything with tools.

| Transport | Manifest | What it is |
|---|---|---|
| `mcp` | `mcp:` | A real Model Context Protocol server. Ava speaks JSON-RPC 2.0 to it over Streamable HTTP, the legacy HTTP+SSE transport (what Home Assistant's MCP Server integration speaks), or stdio against a spawned subprocess. |
| `discover` | `actions.discover` | Ava's own `ava-tools/1` HTTP facade. MCP-*shaped*, but not MCP: Ava GETs your tool list and POSTs `{name, arguments}` to call. |
| `rest` | `actions:` | Statically declared actions proxied to the app's own REST API, with `{tmpl}` path params filled from the call arguments. |

A connector with none of those reports `none`: a UI-only app, or a push-only
device.

??? note "The meta-tool switch: what happens past fifteen tools"

    Past roughly fifteen tools, per-action schemas bloat the agent's context on
    every single turn and tool selection degrades. So Ava collapses large tool
    sets to **exactly two generated tools**:

    ```
    <id>_find_tool   keyword-search this connector's actions
    <id>_call        invoke one by name
    ```

    This happens for every dynamic connector (`mcp:` or `actions.discover`,
    whose tool set is not knowable at generation time), and for any static
    connector declaring **16 or more** actions with a `path`
    (`META_TOOLS_MIN = 16`). Below that threshold you get one tool per action,
    so a static app declaring 15 endpoints gives the agent 15 named tools, while
    the same app at 16 collapses to the two meta-tools, and every
    `mcp:`/`discover` app gets the pair regardless of size.

    The search and the cap run **server-side**, on the bridge: `find_tool`
    filters by keyword, ranks by match count, truncates to the requested limit,
    and reports the pre-cap total so the agent knows to refine. The full schema
    list never enters the sandbox.

    Same routes, same egress policy, same approvals gate. Only the tool shape
    changes. Full detail, including what happens if you mix static and dynamic
    shapes in one manifest, is in the [Connector SDK](../CONNECTOR_SDK.md).

## Permissions: five tiers, decided at call time

Connecting an app never shows you an endpoint review you cannot evaluate.
Consent is just-in-time instead, and the tier of the action decides how it is
asked. Every action carries exactly one tier.

| Tier | What it means | Behaviour |
|---|---|---|
| `read` | No side effects, nothing private | Runs silently. |
| `sensitive` | No side effects, but it discloses something (a mailbox, a chat corpus, a location history) | Asks on **first** use. "Always allow" silences it from then on. |
| `write` | Has side effects | Asks on **first** use. "Always allow" silences it from then on. |
| `destructive` | Irreversible | Asks **every** time. Never grantable. |
| `physical` | Moves something in the real world | Asks **every** time. Never grantable. |

An author's explicit `confirm: true`, on the connector or on one action, always
asks and can never be granted away, whatever the tier.

??? note "Why `sensitive` is its own tier"

    The tiers sit on two independent axes that three tiers conflated: does the
    call have **side effects**, and does it **disclose** something?

    `sensitive` exists because `read` meant "no side effects" and was
    implemented as "runs silently, forever". An author who wanted "ask before
    you read my conversations" had to label a read `write` or `destructive`,
    mislabelling it either way, and in the `destructive` case training you to
    tap through the prompt that actually matters. A silent read from a
    capability group that can also fetch an arbitrary URL is an undisclosed
    disclosure. Enforcement is identical to `write`; the difference is that the
    prompt can now say truthfully what it is asking about.

For a static action the tier is the explicit `access:` if declared, else
inferred: `GET`/`HEAD` becomes `read`; `DELETE`, or "delete" in the action id or
path, becomes `destructive`; everything else becomes `write`. **`physical` is
never inferred.** It moves something in the real world, a relay, a lock, a
valve, so it has to be declared. The code's own justification for making it
ungrantable is that a lock must not become a one-tap-then-silent action.

!!! warning "A dynamically discovered tool has no static declaration, and that is the most security-relevant default in the model"

    It is classified in this order:

    1. The manifest's `dynamic_access` fnmatch patterns, first match wins. A
       matched pattern whose tier is misspelled fails **closed** to
       `destructive` rather than falling through.
    2. The app's own self-reported tier, **but only from an app Ava has reason
       to trust**: a private or loopback address, or an explicit
       `trust_declared_tiers: true`. A remote server's tool list can change
       without you touching Ava, and "quieter" means `read`, and `read` means
       runs-silently-forever, so a remote server does not get to grant itself
       silence on its own word.
    3. Otherwise, **`physical` on a `role: device` connector** and `write`
       everywhere else.

    A device's unknown verbs are presumed to move something until the author
    says otherwise. That is why `home-assistant`, whose tool set is whatever you
    exposed to Assist, reads live state silently but asks every single time
    before it turns anything on, with no way to grant that away.

The operator's manifest always outranks the app's self-report. An app can
declare one of its tools `read`; a `dynamic_access` pattern saying `physical`
overrides it.

An approval **blocks the calling request** on a condition variable until you
answer, for up to 120 seconds. More than 50 parked calls fails closed to
`denied`. Both the request and the outcome are audit events.

### Grants: where "Always allow" is written down

**Setup → Connectors → Permissions** lists every action of an app, grouped by
capability, each with its tier and its grant state. `read` actions read *always
allowed*; `destructive` and `physical` read *asks every time* and have no
control to click; `write` and `sensitive` carry the checkbox that grants or
revokes. That per-action list is the page where you undo an "Always allow".

Grant and revoke are **both** audit events, so "what can this app do, and since
when" is always answerable from the ledger rather than from memory.

!!! note "What you are asked at connect time is coarser"

    The **Connect an app** form has one switch, *Ask me before Ava uses these*,
    which sets `confirm: true` for the whole connector. It is deliberately
    all-or-nothing: at connect time you have not used the app yet, so there is
    nothing to evaluate per tool. The per-action control appears afterwards,
    under Permissions.

??? note "The grants file, and why the agent cannot write it"

    Answering "Always allow" writes a durable grant to
    `$AVA_HOME/connector_grants.yaml`:

    ```yaml
    persona:
      post_persona: {granted: "2026-07-08T20:41:00Z", by: owner}
    ```

    Revoking is deleting the entry, from Setup → Connectors → Permissions or the
    API.

    No agent tool can touch it. Ava has no file tool at all — the repo
    read/write loop went with self-editing — so the grants file is unreachable
    by construction rather than by a deny-list. That matters because the grants
    file is the connector consent ledger: a writable one would be
    self-approval. Ava cannot grant herself a permission you did not give her.

## The trust boundary

This is the part worth being precise about.

**Ava's sandboxed agent never talks to a connector's API, and never talks to an
MCP server.** Not to a local one, not to a remote one. The MCP client runs
host-side, in the bridge. A stdio MCP server runs as a host process you declared
in the manifest, the same trust model as any MCP desktop client, but the agent's
blast radius does not include it.

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
capability-scoped internal token, and the policy engine enforces the route list.
Everything else is denied by the sandbox's network policy.

Every call through that route is recorded twice: as an `egress` audit event with
the connector, the tool and the outcome (the HTTP status when it ran, or
`blocked:denied` / `blocked:timeout` when the approvals gate refused it), and as
latency and status in that app's perf log. Refused calls are on the record too,
which is the half most systems drop. The ledger is `${AVA_LOGS}/audit.jsonl`,
and `ava attest` reports its size and path.

### What contains the server, per transport

The paragraphs above are about the **agent**, and they hold for every MCP
transport. What is contained on the *server* side differs, and it is worth
stating plainly rather than leaving one sentence to cover three cases.

!!! warning "A default `stdio` server runs on your host, as you, with your files"

    | `transport` | What constrains the server |
    |---|---|
    | `http` / `sse` | **Nothing.** The bridge posts to the URL from your manifest. There is no host allow-list and no SSRF guard on this path, unlike Ava's web-search fetch, which re-validates every redirect hop. A remote MCP server is as trusted as the operator who declared it. |
    | `stdio` + `sandbox: docker` | A throwaway container: read-only rootfs, tmpfs `/tmp` and `/root`, `--cpus 1`, `--memory 512m`, `--pids-limit 256`, `no-new-privileges`, no host mounts, and only the `env:` your manifest declares. Network defaults to `bridge` (outbound is open); set `network: none` to cut it. |
    | `stdio` (default) | A host process running as the bridge user, with your filesystem and your network. The one mitigation is that Ava passes it a **stripped environment** rather than `os.environ`, so it does not inherit *other* connectors' credentials or the bridge's own secrets. Anything it legitimately needs, you declare in `env:`. |

    `sandbox: docker` is the setting that makes "contained" true of the server
    as well as the agent. Detecting a start command **runs** that command, so
    the Connect an app form defaults to contained: it ticks the isolation box
    when Docker is available, and when Docker is absent it refuses to detect
    rather than silently downgrading, unless you explicitly choose to run the
    command on the host.

Ava negotiates MCP revision `2025-03-26` (`ava_bridge/mcp_client.py`).

## Devices

A connector is a device if it declares `role: device` **or** an `ingest:` block.
`role: device` groups it under Devices in the UI and switches the dynamic-tool
fallback to `physical`; `ingest:` opens the push channel. They are independent,
and both are additive on top of the normal pull path: a device's `mcp:` or
`actions:` block works exactly as any app's does.

**Pull** is Ava asking. **Push** is your app deciding ("motion detected", "tank
at 12%") and handing Ava the event. The device logic and the decision to notify
both stay in your app.

Push is authenticated with a **per-connector bearer token**, derived from Ava's
root internal token:

```
HMAC-SHA256(internal_token, "ava-ingest:<id>")
```

That is a deliberately **separate namespace** from the `ava-internal:<group>`
tokens Ava's own sandboxed MCP servers hold. An app that can push events cannot
reach `/internal/*`: it holds a token that authorizes exactly one thing. There
is no new secret store, and changing the connector id rotates its token. Read
yours with `ava device token <id>`, or copy it from the ⋯ menu on **Setup →
Connectors** (*Push token*).

```bash
curl -X POST http://127.0.0.1:8096/api/connectors/greenhouse/events \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"type":"reading","name":"soil_moisture","value":18,"unit":"%",
       "severity":"warn","notify":true,"message":"Bed 3 is dry"}'
```

Every accepted event is appended to `${AVA_LOGS}/devices/<id>.jsonl`, enters a
500-entry in-process ring, and if it is `notify`, `warn` or `critical`, is
raised as an alert. Read events back from `GET /api/devices`,
`ava device events <id>`, or the agent's own `device_events` tool.

??? note "The ingest contract: guards, status codes and accepted fields"

    The guards run in this order, and each has its own status code
    (`phone_bridge.py`):

    1. **Bearer token.** Wrong or missing gives `401`.
    2. **`ingest.enabled`** in the manifest. `404` if the connector never opted
       in.
    3. **Token bucket**, per connector: 600 events/min, burst 60, gives `429`.
    4. **Body cap**, 64 KiB, since a single event is tiny, gives `413`.
    5. **JSON parse** gives `400`.
    6. **Field normalization** gives `400` with the reason.

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

    The device log uses the same bounded rotation the perf log uses, under an
    advisory lock, and is best-effort, so a full disk never fails your app's
    push.

    The full walkthrough, including a runnable example app in about 150 lines,
    is in [Device connectors](../DEVICE_CONNECTORS.md).

## Connecting one from the browser

No files and no terminal: **Setup → Connectors → Connect an app**. Name it,
paste its address, let Ava detect its tools, and deploy it. The guided version,
step by step, is
[Connect your apps](../CONNECT_YOUR_APPS.md).

You paste either a web address like `http://127.0.0.1:9000` or a start command
like `npx -y @modelcontextprotocol/server-filesystem ~/notes`, and **Credentials
never enter the manifest**: the manifest stores only the *name* of an
environment variable, and a value you paste is saved once to Ava's server-side
secret store and presented to the agent tools and the embedded UI from there.

Connectors group by identity, not protocol (**Devices**, **Apps**, **Tools**),
because a device that speaks MCP is still a device.

??? note "What Detect probes for, and what each row gives you"

    **Detect** tries, in sequence: a self-describing `/.well-known/ava.json`,
    then MCP over Streamable HTTP, then the `ava-tools/1` facade at `/tools`,
    then a published OpenAPI spec (`/openapi.json`, `/swagger.json`,
    `/openapi`) whose paths it turns into a pre-filled action list. If nothing
    is discoverable, the form lets you declare the actions by hand.

    Each connector row carries its transport badge, its action count, its
    credential state, and a deploy indicator that appears only when the
    generated tools or egress policy have drifted from the manifest. From the
    row you get:

    - **Permissions**, the per-action tier and grant list described above. This
      is where you revoke an "Always allow".
    - **Preview**, the exact tools and egress policy the manifest renders,
      without touching the agent.
    - **Deploy** / **Redeploy**, which regenerates both into the sandbox.
    - **⋯**, holding the push token, the credential, **Appearance** (icon and
      accent colour), an inline manifest editor, disable and remove.

??? note "Manifest robustness: what happens when your YAML is wrong"

    A connector manifest is a file *you* wrote, so the loader treats it as such.

    - **Per-block quarantine, never per-connector.** If a block is the wrong
      type, `egress:` as a string instead of a mapping, that block is dropped
      from the **in-memory** copy and reported, and the rest of the connector
      loads. Your file is never rewritten. The reason is concrete: the one
      screen that can fix a broken manifest is Setup → Connectors, which is also
      the screen a crash on `egress:` would take down.
    - **Unknown top-level keys warn**, rather than silently doing nothing, which
      is how `egres:` goes unnoticed forever. Prefix your own keys with `x_` to
      silence the warning.
    - **A newer `manifest_version` still loads**, with a warning that unknown
      blocks are ignored. Forward-compatibility by ignoring what it does not
      understand is what keeps a fork's manifests portable.
    - **A malformed YAML file never crashes boot.** It lands in the load-error
      list, which is surfaced at the top of Setup → Connectors and in
      `ava doctor`, so a bad manifest is visible rather than absent.

## Where to go next

- [**Connect your apps**](../CONNECT_YOUR_APPS.md) is the guided no-code
  walkthrough.
- [**Connector SDK**](../CONNECTOR_SDK.md) is the full manifest reference, the
  `ava-tools/1` facade contract, embed tiers, and a runnable example.
- [**Device connectors**](../DEVICE_CONNECTORS.md) covers the pull and push
  paths for your own hardware.
- [**Connect your Home Assistant**](../CONNECT_HOME_ASSISTANT.md) is one
  manifest and two environment variables.
- [**The agent**](agent.md) is the sandbox on the other side of the boundary,
  and the capability-scoped tokens that police it.
- [**Security**](../../SECURITY.md) is the trust model end to end.
