# Connect your Home Assistant

Ava becomes the **private, local AI brain** over the devices your Home Assistant
already manages: ask about your home in plain language, and let Ava act on it —
with **every physical action approved by you, every time**.

The integration is one connector folder. Ava core has no Home Assistant code,
dependency, or assumption: HA is an app *you* connect, exactly like any other
connector. Delete the folder and every trace is gone.

**What you get**

- **Ask about your home** — "is the garage door open?", "what's the temperature
  upstairs?" run silently against HA's live state (`GetLiveContext`).
- **Act on your home, governed** — "turn off the kitchen lights" pauses on an
  approval prompt. Actuation is the **`physical` tier: it asks every time and
  can never be silenced with "Always allow."** A lock cannot become a
  one-tap-then-silent action.
- **On the record** — every request, approval, denial, and call is written to
  the audit ledger (Hub → History).
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

```bash
# 1. Drop the connector into your data root
cp -r examples/home-assistant "$AVA_HOME/connectors/home-assistant"

# 2. Point it at your HA (Ava's .env, or however you set Ava's environment)
HASS_URL=http://homeassistant.local:8123
HASS_TOKEN=<your long-lived token>

# 3. Restart Ava, then render + install the connector's egress policy
ava up
ava connector policies home-assistant --write
(cd agent && ./install.sh)
```

Home Assistant now appears under Devices, with a health row on the Ops
dashboard. Ask Ava something only your house knows.

> Unset `HASS_URL` and the connector is **inert** — it registers nothing and
> reaches nothing. Remove the folder to disconnect entirely.

## The trust model

HA's MCP server exposes its Assist tools; Ava discovers them live and classifies
each by the manifest's `dynamic_access` patterns:

| Tool | Tier | Behavior |
|---|---|---|
| `GetLiveContext` | `read` | runs silently — this is the "monitor" half |
| everything else (`HassTurnOn`, …) | `physical` | **asks every time; never grantable** |

To relax a specific benign tool, edit *your copy* of the manifest
(`$AVA_HOME/connectors/home-assistant/connector.yaml`):

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
  HA, or `HASS_URL` points at the wrong host/port.
- **"SSE connect returned 401"** — bad or expired `HASS_TOKEN`.
- **Tools list is empty** — you haven't exposed any entities to Assist in HA.
- **Ava describes stale state** — `GetLiveContext` reads live; check the HA
  health row on the Ops dashboard (probe = your `HASS_URL`).

## Scope, honestly

Today this is **ask-and-act**: you talk to Ava; reads are silent, actuation is
approval-gated. Ava does not yet subscribe to HA's state-change events or act
proactively on them — that (an opt-in, governed trigger loop) is planned work,
tracked in the roadmap. Ava also does not replace HA's automations: keep those
in HA; Ava adds reasoning and governance on top.
