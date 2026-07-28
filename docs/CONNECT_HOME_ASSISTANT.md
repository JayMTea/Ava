# Connect your Home Assistant

Ava becomes the **private, local AI brain** over the devices your Home Assistant
already manages: ask about your home in plain language, and let Ava act on it —
with **every physical action approved by you, every time**.

The integration is one connector folder. Ava core has no Home Assistant code,
dependency, or assumption: HA is an app *you* connect, exactly like any other
connector. Disable it in Setup → Connectors, or delete the manifest folder, and
every trace is gone.

**What you get**

- **Ask about your home** — "is the garage door open?", "what's the temperature
  upstairs?" run silently against HA's live state (`GetLiveContext`).
- **Act on your home, governed** — "turn off the kitchen lights" pauses on an
  approval prompt. Actuation is the **`physical` tier: it asks every time and
  can never be silenced with "Always allow."** A lock cannot become a
  one-tap-then-silent action.
- **On the record** — every request, approval, denial, and call is written to
  the audit ledger (Setup → History).
- **Sandboxed reach** — Ava's agent never talks to HA directly. The bridge
  speaks MCP server-side; the agent reaches only the two policed bridge routes
  its egress policy allow-lists.

## Prerequisites

1. A running Home Assistant (2025.2 or later) with the
   **Model Context Protocol Server** integration added
   (Settings → Devices & services → Add integration → *MCP Server*).
2. Entities [exposed to Assist](https://www.home-assistant.io/voice_control/voice_remote_expose_devices/)
   — only what you expose there is visible to Ava. Start small.
3. A **long-lived access token** (HA: your profile → Security → Create token).

## Connect

There is nothing to install: the connector **ships with Ava**
(`connectors/home-assistant/`) and is already registered. It stays inert until
you point it at your HA.

```bash
# 1. Point it at your HA (Ava's .env, or however you set Ava's environment)
HASS_URL=http://homeassistant.local:8123
HASS_TOKEN=<your long-lived token>

# 2. Restart Ava so it picks up the env vars
systemctl --user restart ava-bridge    # or Ctrl-C and re-run `ava up`, which
                                       # holds the terminal until you stop it

# 3. Render the connector's agent tools + egress policy and install them
#    (in a second terminal, if `ava up` is holding this one)
ava connector tools    home-assistant --write
ava connector policies home-assistant --write
(cd agent && ./install.sh)
```

Home Assistant's row under Devices now comes alive, with a health row on the Ops
dashboard. Ask Ava something only your house knows.

> Unset `HASS_URL` and the connector is **inert** — it exposes no tools and
> renders no egress policy, so Ava can reach nothing. It still shows as an
> unconfigured row under Devices and in Setup → Connectors; disable it there to
> hide it entirely.

## The trust model

HA's MCP server exposes its Assist tools; Ava discovers them live and classifies
each by the manifest's `dynamic_access` patterns:

| Tool | Tier | Behavior |
|---|---|---|
| `GetLiveContext` | `read` | runs silently — this is the "monitor" half |
| everything else (`HassTurnOn`, …) | `physical` | **asks every time; never grantable** |

To relax a specific benign tool, edit the manifest
(`connectors/home-assistant/connector.yaml` — or a copy at
`$AVA_HOME/connectors/home-assistant/connector.yaml`, which overrides the
shipped one by id):

```yaml
dynamic_access:
  GetLiveContext: read
  HassListAddItem: write     # shopping list — ask once, then "Always allow" works
  "*": physical
```

Tier semantics are the standard JIT-consent rules (see
[Connector SDK §5](CONNECTOR_SDK.md)): `read` silent · `write` asks once,
grantable · `destructive`/`physical` ask every time, never grantable · an
author `confirm:` always sticks. The permission sheet for the connector
(Setup → Connectors → Home Assistant) lists every live tool with its tier.

## Troubleshooting

- **"SSE connect returned 404"** — the MCP Server integration isn't added in
  HA, or `HASS_URL` points at the wrong host/port. The shipped manifest talks
  to HA's legacy SSE endpoint (`${HASS_URL}/mcp_server/sse`); current HA also
  serves the newer Streamable HTTP endpoint, so you can point your own copy at
  `url: "${HASS_URL}/api/mcp"` instead.
- **"SSE connect returned 401"** — bad or expired `HASS_TOKEN`.
- **Tools list is empty** — you haven't exposed any entities to Assist in HA.
- **Ava describes stale state** — `GetLiveContext` reads live; check the HA
  health row on the Ops dashboard (probe = your `HASS_URL`).

## Scope, honestly

Today this is **ask-and-act**: you talk to Ava; reads are silent, actuation is
approval-gated. Ava does not subscribe to HA's state-change events and does not
act on its own. Ava also does not replace HA's automations: keep those
in HA; Ava adds reasoning and governance on top.
