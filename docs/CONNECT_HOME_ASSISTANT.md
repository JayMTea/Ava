# Connect your Home Assistant

Ava becomes the **private, local AI brain** over the devices your Home Assistant
already manages: ask about your home in plain language, and let Ava act on it -
with **every physical action approved by you, every time**.

The integration is one connector folder (`examples/home-assistant/`, which you
copy into your own `$AVA_HOME/connectors/`). Ava core has no Home Assistant code,
dependency, or assumption - and ships nothing HA-shaped enabled: HA is an app
*you* connect, exactly like any other connector. Disable it in Setup →
Connectors, or delete the manifest folder, and every trace is gone.

**What you get**

- **Ask about your home** - "is the garage door open?", "what's the temperature
  upstairs?" run silently against HA's live state (`GetLiveContext`).
- **Act on your home, governed** - "turn off the kitchen lights" pauses on an
  approval prompt. Actuation is the **`physical` tier: it asks every time and
  can never be silenced with "Always allow."** A lock cannot become a
  one-tap-then-silent action.
- **On the record** - every request, approval, denial, and call is written to
  the audit ledger (Data → History).
- **Sandboxed reach** - Ava's agent never talks to HA directly. The bridge
  speaks MCP server-side; the agent reaches only the two policed bridge routes
  its egress policy allow-lists.

## Prerequisites

1. A running Home Assistant (2025.2 or later) with the
   **Model Context Protocol Server** integration added
   (Settings → Devices & services → Add integration → *MCP Server*).
2. Entities [exposed to Assist](https://www.home-assistant.io/voice_control/voice_remote_expose_devices/).
   Only what you expose there is visible to Ava. Start small.
3. A **long-lived access token** (HA: your profile → Security → Create token).

## Connect

The connector is **one manifest that ships in the repo as a template** -
`examples/home-assistant/`. Ava does not register it for you (nothing
Home-Assistant-shaped is enabled in a fresh install), so step 1 is to copy that
folder into your own connectors directory. Register it, point it at your HA, and
it comes alive; delete the folder and every trace is gone.

```bash
# 1. Register the connector (copy the shipped template into YOUR $AVA_HOME).
#    mkdir -p first: a fresh $AVA_HOME has no connectors/ directory yet.
mkdir -p "$AVA_HOME/connectors"
cp -r examples/home-assistant "$AVA_HOME/connectors/home-assistant"

# 2. Point it at your HA. These are two lines in a FILE, not shell commands —
#    typing them at a prompt sets nothing Ava can see. Ava reads `.env` next to
#    its code, and `$AVA_HOME/.env`:
cat >> .env <<'EOF'
HASS_URL=http://homeassistant.local:8123
HASS_TOKEN=<your long-lived token>
EOF

# 3. Restart Ava so it picks up the connector and the env vars.
#    Ctrl-C and re-run `ava up` (it holds the terminal until you stop it), or
#    `cd deploy && docker compose restart ava` for the container install.
#    (Ava ships no systemd unit — if you wrote your own, restart that.)

# 4. Render the connector's agent tools + egress policy and install them
#    (in a second terminal, if `ava up` is holding this one).
ava connector tools    home-assistant --write
ava connector policies home-assistant --write
(cd agent && ./install.sh)
```

**If step 1 or 2 didn't take, step 4 tells you nothing.** With the connector
unregistered (or `HASS_URL` unset), both commands print `0 tool(s) written` /
`0 policy file(s)` and **exit 0** - the same output as success with nothing to do.
Check with `ava connector list | grep home-assistant` before you go looking for a
bug; a registered, configured connector shows its probe URL there.

Home Assistant's row now comes alive in Setup → Connectors (under **Tools** - a
`role: device` connector is listed by the Devices registry that `ava device list`
and `GET /api/devices` read, but the Setup grouping keys off `kind:`), with a
health row on Operations → Service health. Ask Ava something only your house
knows.

!!! note "An unconfigured connector reaches nothing"

    Unset `HASS_URL` and the connector is **inert** - it exposes no tools and
    renders no egress policy, so Ava can reach nothing. It still shows as an
    unconfigured row in Setup → Connectors; disable it there to hide it
    entirely.

## The trust model

HA's MCP server exposes its Assist tools; Ava discovers them live and classifies
each by the manifest's `dynamic_access` patterns:

| Tool | Tier | Behavior |
|---|---|---|
| `GetLiveContext` | `read` | runs silently - this is the "monitor" half |
| `GetDateTime` | `read` | runs silently - informational |
| `todo_get_items` | `read` | runs silently - reads a to-do/shopping list |
| everything else (`HassTurnOn`, `HassLockLock`, …) | `physical` | **asks every time; never grantable** |

To relax a specific benign tool, edit your copy of the manifest
(`$AVA_HOME/connectors/home-assistant/connector.yaml`). Patterns are fnmatch and
**the first match wins**, so a relaxed tool must be listed *above* the `"*"`
catch-all, and keep the read entries you still want silent - dropping them
demotes them to `physical`, which means Ava starts asking permission to check the
time:

```yaml
dynamic_access:
  GetLiveContext: read
  GetDateTime: read
  todo_get_items: read
  HassListAddItem: write     # shopping list — ask once, then "Always allow" works
  "*": physical
```

(A manifest in `$AVA_HOME/connectors/<id>/` overrides a same-`id` manifest shipped
in the repo's `connectors/` - whole file, not per key. Home Assistant ships no
repo-side manifest, so your copy is the only one.)

Tier semantics are the standard JIT-consent rules (see
[Connector SDK §5](CONNECTOR_SDK.md)): `read` silent · `write` asks once,
grantable · `destructive`/`physical` ask every time, never grantable · an
author `confirm:` always sticks. The permission sheet for the connector
(Setup → Connectors → Home Assistant) lists every live tool with its tier.

## Troubleshooting

- **"SSE connect returned 404"** - Ava reached the host but there's nothing at
  `/mcp_server/sse`: the MCP Server integration isn't added in HA, or `HASS_URL`
  has a path/prefix that shouldn't be there. It must be the bare origin
  (`http://homeassistant.local:8123`) - the manifest appends the path.
- **"home-assistant mcp: … Connection refused"** - `HASS_URL` points at the wrong
  host or port (HA's default is `8123`), or HA isn't running. A wrong port gives
  you a connection error, *not* a 404.
- **"SSE connect returned 401"** - bad or expired `HASS_TOKEN`.
- **Tools list is empty** - you haven't exposed any entities to Assist in HA.
- **Prefer the newer transport?** The template talks to HA's legacy HTTP+SSE
  endpoint (`${HASS_URL}/mcp_server/sse`), which is what the MCP Server
  integration has always served. Ava picks the legacy transport from the `/sse`
  suffix and Streamable HTTP otherwise, so *if* your HA version serves a
  Streamable HTTP endpoint you can change your copy's `mcp.url` to it (check your
  HA release notes for the path - Ava doesn't care which it is).
- **Ava describes stale state** - `GetLiveContext` reads live; check the HA
  health row on the Ops dashboard (probe = your `HASS_URL`).

## Scope, honestly

Today this is **ask-and-act**: you talk to Ava; reads are silent, actuation is
approval-gated. Ava does not subscribe to HA's state-change events and does not
act on its own. Ava also does not replace HA's automations: keep those
in HA; Ava adds reasoning and governance on top.
