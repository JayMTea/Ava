# Device connectors: wire your own sensing devices to Ava

Bring your **own** app for your **own** hardware (Arduino, Nicla, Portenta,
ESP32, a smart-home hub, any sensor) and connect it to Ava by dropping in a
folder. No edits to Ava's code, and no device protocols baked into Ava.

At the end you can ask Ava *"what's the greenhouse temperature?"* and *"did
anything happen with my devices?"*, and Ava can raise an alert on your dashboard
when your device decides something happened.

## The idea: Ava speaks to your app, not to your hardware

Ava deliberately does **not** speak serial, BLE, MQTT, Zigbee, Matter, or any
board-level protocol. Your app does. That is what keeps this general: any device,
any transport, no Ava release needed. Ava connects to your app generically, in
two directions:

| Direction | Who starts it | What it's for | Mechanism |
|---|---|---|---|
| **Pull** | Ava | read a sensor / send a command on demand | the connector `actions.discover` bridge (`GET /tools`, `POST /call`) |
| **Push** | your app | hand Ava an event when *it* decides (motion, threshold, a reading) | `POST /api/connectors/<id>/events` with a per-connector token |

A **device connector** is just a normal [connector](CONNECTOR_SDK.md) whose
`connector.yaml` adds an `ingest:` block (push) and usually an `actions.discover`
block (pull). Everything else in the Connector SDK (health probe, left-rail UI,
egress policy) still applies.

!!! note "Already running Home Assistant? That's the worked example"

    Home Assistant is a device connector you do not have to write. Its own MCP
    Server integration plugs straight into the SDK's `mcp:` block, with
    actuation gated behind the never-grantable `physical` tier. Everything on
    this page - the two directions, the consent tiers, the egress policy - is
    what that connector is made of.

    Follow [Connect your Home Assistant](CONNECT_HOME_ASSISTANT.md) if that is
    your case. Stay here if you are wiring your own hardware or your own app.

Here is the pull path from the browser - connecting a device's tool server -
narrated (sound on). The push half (token, `POST …/events`) is covered below:

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Narrated screen recording: connecting a device's tool server to Ava from the browser">
  <source src="../assets/connect-device-tour.mp4" type="video/mp4">
  <track kind="captions" srclang="en" label="English"
         src="../assets/connect-device-tour.vtt">
  Your browser can't play video. <a href="../assets/connect-device-tour.mp4">Download the walkthrough</a>.
</video>

---

## The manifest

```yaml
id: greenhouse
label: Greenhouse
kind: app
role: device                 # NOT cosmetic: groups it in Ava's Devices view AND
                             # makes unmatched dynamic tools default to the
                             # never-grantable `physical` tier (CONNECTOR_SDK §5)

service:                     # optional — Ops dashboard health row
  name: Greenhouse
  probe: "http://127.0.0.1:9001/health"

# PULL — Ava reads/commands your devices on demand. Your app exposes an MCP-style
# tool server; Ava bridges the whole set live (no per-tool wiring).
actions:
  discover:
    base: "http://127.0.0.1:9001"
    list: "/tools"           # GET  -> {"tools":[{name,description,inputSchema}]}
    call: "/call"            # POST {name, arguments} -> {"text": "..."}

# PUSH — let your app hand Ava events when IT decides.
ingest:
  enabled: true
  channels:                  # OPTIONAL, purely descriptive (nicer Devices view)
    - { name: temperature, unit: "C" }
    - { name: motion, kind: event }

# What Ava's sandboxed agent may reach for the PULL path (auto-rendered to an
# egress policy by `ava connector policies greenhouse --write`).
egress:
  routes:
    - "GET /internal/connector/greenhouse/__tools"
    - "POST /internal/connector/greenhouse/__call"
```

---

## Pull: Ava reads and commands your devices

Your app exposes the same small MCP-style contract as
[`examples/hello-app/`](../examples/hello-app/):

```
GET  /tools                    -> {"tools": [ {name, description, inputSchema}, ... ]}
POST /call {name, arguments}   -> {"text": "..."}
```

Ava discovers those tools live (via `actions.discover`) and the agent calls them
when the user asks: *"what's the greenhouse temperature?"*, *"turn on the pump"*.
This path is unchanged from the Connector SDK; see [CONNECTOR_SDK.md §5](CONNECTOR_SDK.md).

To generate the agent tools and the egress allow-list so the sandboxed agent may
reach the bridge proxy for this app:

```bash
ava connector tools    greenhouse --write   # -> agent/mcp_server_connectors/apps/greenhouse/
ava connector policies greenhouse --write   # -> agent/policies/generated/greenhouse.yaml
cd agent && ./install.sh                    # deploy into the sandbox
```

The generator already derives the two `/internal/connector/<id>/__tools|__call` rules
from `actions.discover`, so the `egress.routes` above are optional - listing them
just repeats what is generated.

!!! warning "Ava asks before it moves anything in the real world"

    A `role: device` connector's tools are gated by just-in-time consent. The
    first call parks an approval prompt in the dashboard and the call *blocks*
    until you approve it (or 403s with `not run — awaiting-approval timeout`
    after 120s).

    `role: device` also makes an unmatched dynamic tool default to the
    **`physical`** tier, which asks **every** time and can never be granted away
    with "Always allow". Declare a `dynamic_access` `"*": physical` catch-all to
    keep it that way for tools Ava has already discovered - see
    [CONNECTOR_SDK.md §5](CONNECTOR_SDK.md).

    Expect the block when you test the pull path from a script instead of from
    the UI.

---

## Push: your app hands Ava an event

When your app decides something happened, it POSTs to Ava:

```
POST {server.public_url}/api/connectors/<id>/events
Authorization: Bearer <token from `ava device token <id>`>
Content-Type: application/json

{ "type": "event",              // "event" | "reading"   (default "event")
  "name": "motion",              // required — the channel/sensor name
  "value": 1, "unit": null,      // for readings
  "message": "Front door motion",// human text (used for the notification)
  "severity": "warn",            // info | warn | critical   (optional)
  "notify": true,                // surface to the user now?  (default false)
  "ts": 1720000000 }             // optional; Ava stamps it if absent
```

Returns `{ "ok": true, "seq": N }`. The other answers you can get:

| Code | Means |
|---|---|
| 401 `unauthorized` | wrong/missing bearer for **this** connector id |
| 404 `connector <id> has not enabled ingest` | no `ingest.enabled: true`, or Ava hasn't reloaded the manifest yet (restart) |
| 413 `event too large` | body over 64 KiB |
| 429 `rate limited` | per-connector token bucket: ~600 events/min, bursts to 60 (`AVA_DEVICES_RATE_PER_MIN`, `AVA_DEVICES_BURST`) |
| 400 `invalid json` / a field error | unusable payload |
| 421 `untrusted host` | Ava doesn't answer to the `Host:` your device sent - see below |

Batch or downsample on your side if you sample faster than that - a 429'd event
is dropped, not queued.

???+ note "Pushing from another machine (a board on your LAN) - read this before you debug a 421"

    Ava only answers to `Host:` names it recognises: loopback, `server.host`,
    the host in `server.public_url`, and anything in `server.trusted_hosts`. A
    device that posts to Ava's LAN address sends `Host: 192.168.1.50`, which a
    default install does **not** recognise - every push comes back
    `421 untrusted host` with a DNS-rebinding explanation. Name the address your
    devices use, in `ava.yaml`:

    ```yaml
    server:
      public_url: "http://192.168.1.50:8096"   # or:
      trusted_hosts: ["192.168.1.50", "ava.your-tailnet.ts.net"]
    ```

    Either one is enough; restart Ava afterwards. (Loopback-only devices - a
    host adapter or app on the same machine - need none of this.)

What Ava does with an accepted event:

- **Stores** it in `$AVA_HOME/logs/devices/<id>.jsonl` (the logs root honours
  `paths.logs` / `AVA_LOGS_DIR`). Bounded and self-managing - 8 MiB per file,
  3 rotations kept, no external database - so it survives restarts and Ava can
  answer *"did anything happen?"*.
- **Surfaces it live** on the dashboard: every event rides the ops SSE stream as
  a `device.event` frame and lands at the top of the **Device events** panel on
  the Operations page (the panel appears once your first event arrives).
- For **`notify: true`** or **`severity` warn/critical**, also raises a short-lived
  entry in the dashboard's **active alerts** panel.
- Makes it **readable by Ava's agent** via the `device_events` tool, so *"did the
  greenhouse report anything?"* works in conversation.

### Authentication

Each connector has its own inbound **bearer token**, derived from Ava's root
secret (`HMAC(root, "ava-ingest:<id>")`). Print it with `ava device token <id>`,
or copy it from **Setup → Connectors → ⋯ → Push token**.

`ava device token` prints a human-readable block (the token plus a ready-made
`curl`), so it is *not* pipe-safe - `$(ava device token <id>)` captures the whole
block. To put just the token in an env var:

```bash
export AVA_INGEST_TOKEN="$(ava device token greenhouse | sed -n 3p | tr -d '[:space:]')"
```

It is deliberately **not** Ava's internal tool token: an app that can push events
**cannot** reach Ava's `/internal/*` tool surface. Rotating the connector id
rotates the token. Keep it secret; treat it like a password.

!!! note "Who decides to notify"

    The *decision* to notify is your app's; your device logic owns when to fire.
    Ava is the *delivery surface*: it receives the event and shows it to the
    user.

    Opt-in **voice**: tick *Speak alerts* in the Operations page's **Device
    events** panel and Ava reads notify/warn/critical events aloud in that
    browser via the Web Speech API - no server voice process. Per-browser, and
    off by default.

---

## Worked example: the demo device app

[`examples/device-app/`](../examples/device-app/) is a complete, runnable device
connector: both directions in about 150 lines of stdlib Python. `server.py`
stands in for your app (tools: `read_temperature`, `set_relay`, plus a demo
event pusher); replace the faked device I/O with yours.

```bash
# 1. Register the connector (the example's manifest declares id: device-demo)
#    then restart Ava (or `ava up`) — a running Ava answers 404 "has not enabled
#    ingest" until it reloads the new manifest, so do this BEFORE the app pushes.
mkdir -p "${AVA_HOME:-$PWD}/connectors"          # ava setup does not create this
cp -r examples/device-app "$AVA_HOME/connectors/device-demo"

# 2. Get this connector's inbound push token
ava device token device-demo        # copy the token it prints

# 3. Start your app, pointed at Ava
export AVA_URL=http://localhost:8096
export AVA_CID=device-demo
export AVA_INGEST_TOKEN=<the token from step 2>
python3 examples/device-app/server.py    # serves 127.0.0.1:8479 (DEVICE_APP_PORT)

# 4. Generate its agent tools + egress policy and load them into the sandbox
#    (or use Setup -> Connectors -> Deploy)
ava connector tools    device-demo --write
ava connector policies device-demo --write
cd agent && ./install.sh

# 5. Then, with Ava running:
ava device list                     # shows device-demo (pull,push)
ava device events device-demo       # watch the pushed motion + readings arrive
```

Ask Ava *"read the demo temperature"* (pull) or *"did anything happen with my
devices?"* (push, read via the `device_events` tool). The `notify`/warn motion
event also raises a dashboard alert.

---

## What Ava does NOT do

- No serial/BLE/MQTT/Firmata/Matter/Zigbee transports, and no shipped firmware.
  Your app owns all device I/O, so this stays general for *any* device.
- No automation or rules engine. The "if this then that" lives in your app,
  which is also what decides when to push a `notify` event.

Smart-home gear fits the same contract, and the automation stays where it
already lives.

---

## Where to next

- **Skip writing the bridge**, if the hardware is already in Home Assistant:
  [Connect your Home Assistant](CONNECT_HOME_ASSISTANT.md) is this page's worked
  example, using HA's own MCP Server integration.
- **Every other manifest field** (health probe, left-rail UI, credentials,
  approval tiers): [Connector SDK](CONNECTOR_SDK.md).

??? note "Code that implements this page"

    - `ava_bridge/devices.py` - the event store and the rate limiter.
    - The ingest route and the `device.event` SSE frame - `phone_bridge.py`.
    - `ava_bridge/internal.ingest_token` - the per-connector bearer.
    - `agent/mcp_server_connectors/devices/device_events.mjs` - the agent tool.
