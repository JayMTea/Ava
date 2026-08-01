# Example device app

A complete, runnable **third-party device connector**: the copy-paste starting
point for wiring your own hardware (Arduino, Nicla, Portenta, ESP32, a smart-home
hub, any sensor) to Ava. It shows **both directions** in about 150 lines of
stdlib Python:

- **Pull**: Ava reads and commands your devices on demand. The app exposes an
  MCP-style tool server (`GET /tools`, `POST /call`) that Ava bridges live via the
  connector's `actions.discover` block. Tools here: `read_temperature`, `set_relay`.
- **Push**: your app hands Ava an event when *it* decides (a `motion` event, a
  `temperature` reading), by POSTing to `/api/connectors/<id>/events` with a
  per-connector bearer token.

`server.py` stands in for **your** app: replace the faked `read_temperature()` /
`set_relay()` and the `_demo_pusher()` with your real device I/O.

## Run it

```bash
# 1. Register the connector (built-in id: device-demo)
mkdir -p "${AVA_HOME:-$PWD}/connectors"          # ava setup does not create this
cp -r examples/device-app "$AVA_HOME/connectors/device-demo"

# 2. Get this connector's inbound push token
ava device token device-demo        # copy the token it prints

# 3. Start your app, pointed at Ava
export AVA_URL=http://localhost:8096
export AVA_CID=device-demo
export AVA_INGEST_TOKEN=<the token from step 2>
python3 examples/device-app/server.py

# 4. Restart Ava (or `ava up`) so it loads the connector, then:
ava device list                     # shows device-demo (pull,push)
ava device events device-demo       # watch the pushed motion + readings arrive
```

Ask Ava *"read the demo temperature"* (pull) or *"did anything happen with my
devices?"* (the agent reads the pushed events via the `device_events` tool).
Pushed events also surface live on the dashboard (a `device.event` SSE frame),
and the `notify`/`warn` motion event raises a dashboard alert.

See [docs/DEVICE_CONNECTORS.md](../../docs/DEVICE_CONNECTORS.md) for the full
contract.
