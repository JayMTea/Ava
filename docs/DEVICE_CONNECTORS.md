# Device connectors: wire your own sensing devices to Ava

> Bring your **own** app for your **own** hardware (Arduino, Nicla, Portenta,
> ESP32, a smart-home hub, any sensor) and connect it to Ava by dropping in a
> folder. **No edits to Ava's code, and no device protocols baked into Ava.**

Ava deliberately does **not** speak serial, BLE, MQTT, Zigbee, Matter, or any
board-level protocol. Your app does. Ava connects to your app generically, in two
directions:

| Direction | Who starts it | What it's for | Mechanism |
|---|---|---|---|
| **Pull** | Ava | read a sensor / send a command on demand | the connector `actions.discover` bridge (`GET /tools`, `POST /call`) |
| **Push** | your app | hand Ava an event when *it* decides (motion, threshold, a reading) | `POST /api/connectors/<id>/events` with a per-connector token |

A **device connector** is just a normal [connector](CONNECTOR_SDK.md) whose
`connector.yaml` adds an `ingest:` block (push) and usually an `actions.discover`
block (pull). Everything else in the Connector SDK (health probe, left-rail UI,
egress policy) still applies.

Here is the whole flow, end to end, narrated (sound on):

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Narrated screen recording: connecting a sensing device to Ava end to end">
  <source src="../assets/connect-device-tour.mp4" type="video/mp4">
  Your browser can't play video. <a href="../assets/connect-device-tour.mp4">Download the walkthrough</a>.
</video>

---

## The manifest

```yaml
id: greenhouse
label: Greenhouse
kind: app
role: device                 # groups it in Ava's Devices view (cosmetic)

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

To also generate the egress allow-list so the sandboxed agent may reach the bridge
proxy for this app:

```bash
ava connector policies greenhouse --write   # -> agent/policies/generated/greenhouse.yaml
cd agent && ./install.sh                     # deploy into the sandbox
```

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

Returns `{ "ok": true, "seq": N }`. What Ava does with it:

- **Stores** it in `${AVA_LOGS}/devices/<id>.jsonl` (bounded and self-managing,
  with no external database), so it survives restarts and Ava can answer *"did
  anything happen?"*.
- **Surfaces it live** on the dashboard: every event rides the ops SSE stream as
  a `device.event` frame (a toast).
- For **`notify: true`** or **`severity` warn/critical**, also raises a short-lived
  entry in the dashboard's **active alerts** panel.
- Makes it **readable by Ava's agent** via the `device_events` tool, so *"did the
  greenhouse report anything?"* works in conversation.

### Authentication

Each connector has its own inbound **bearer token**, derived from Ava's root
secret (`HMAC(root, "ava-ingest:<id>")`). Print it with `ava device token <id>`.
It is deliberately **not** Ava's internal tool token: an app that can push events
**cannot** reach Ava's `/internal/*` tool surface. Rotating the connector id
rotates the token. Keep it secret; treat it like a password.

> **Who decides to notify.** The *decision* to notify is your app's; your device
> logic owns when to fire. Ava is the *delivery surface*: it receives the event
> and shows it to the user. (Proactive **voice** announcement isn't wired in v1;
> events surface in the UI and to the agent. The `device.event` frame is the seam
> a future release can hook TTS onto.)

---

## Worked example: the demo device app

[`examples/device-app/`](../examples/device-app/) is a complete, runnable device
connector: both directions in about 150 lines of stdlib Python. `server.py`
stands in for your app (tools: `read_temperature`, `set_relay`, plus a demo
event pusher); replace the faked device I/O with yours.

```bash
# 1. Register the connector (built-in id: device-demo)
cp -r examples/device-app "$AVA_HOME/connectors/device-demo"

# 2. Get this connector's inbound push token
ava device token device-demo        # copy the token it prints

# 3. Start your app, pointed at Ava
export AVA_URL=http://localhost:8096
export AVA_CID=device-demo
export AVA_INGEST_TOKEN=<the token from step 2>
python examples/device-app/server.py

# 4. Restart Ava (or `ava up`) so it loads the connector, then:
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

Smart-home gear (Home Assistant, etc.) fits the same contract: run a small app
that bridges your hub's API to Ava as a device connector.

---

## Reference

- Connector SDK (the base): [CONNECTOR_SDK.md](CONNECTOR_SDK.md)
- Code: `ava_bridge/devices.py` (store), the ingest route + `device.event` in
  `phone_bridge.py`, `ava_bridge/internal.ingest_token` (token),
  `agent/mcp_server_content/devices/device_events.mjs` (agent tool).
