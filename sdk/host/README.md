# Host adapters

Two stdlib-only packages you can copy next to your own code:

| Package | For |
|---|---|
| [`ava_device`](#ava_device-host-adapter) (below) | wiring hardware to Ava — push readings, answer pulls |
| [`ava_mcp`](ava_mcp/README.md) | serving your app's `ava-tools/1` facade as a **real MCP server** |

---

# ava_device (host adapter)

The board-agnostic, reusable version of `examples/device-app/server.py`. It owns
the two contracts Ava speaks for [device connectors](../../docs/DEVICE_CONNECTORS.md)
so you write only your transport code (serial, MQTT, BLE, …).

Use it when your board has no networking of its own (a USB Arduino), or when you
prefer a small host process to bridge your hardware to Ava.

## Install

Stdlib only. Copy the `ava_device/` package next to your script, or add this
folder to `PYTHONPATH`. `SerialRelay` additionally needs `pip install pyserial`.

## Push

```python
from ava_device import AvaDevice

ava = AvaDevice("http://localhost:8096", "greenhouse", TOKEN)  # ava device token greenhouse
ava.reading("temperature", 21.5, "C")
ava.event("motion", "Front door", severity="warn", notify=True)
```

`ava device token` prints a human-readable block, so `$(ava device token …)` is
**not** the token — capture just the token:

```bash
export AVA_TOKEN="$(ava device token greenhouse | sed -n 3p | tr -d '[:space:]')"
```

`reading()` / `event()` return `False` instead of raising when Ava refuses a push,
and print the status: `401` (wrong token), `404` (the connector has no
`ingest.enabled: true`, or Ava hasn't reloaded its manifest), `429` (Ava's
per-connector rate limit — ~600 events/min, bursts to 60). A `4xx` is not
retried, so downsample on your side rather than pushing faster than that.

## Pull (the facade Ava's `actions.discover` points at)

```python
from ava_device import Tool, serve_pull

serve_pull([
    Tool("read_temperature", "Read the temperature",
         run=lambda args: f"{read_my_sensor():.1f} C"),
    Tool("set_relay", "Turn the relay on/off",
         run=lambda args: set_my_relay(bool(args.get("on"))),
         schema={"on": {"type": "boolean"}}, required=["on"]),
], port=9001)
```

## Bridge a USB Arduino (both directions)

Pair with the Arduino library's `SerialBridge` sketch. See
[`examples/serial_bridge.py`](examples/serial_bridge.py): it reads the board's
advertised tools over serial, exposes them as the pull facade, and forwards the
board's pushed readings/events to Ava — about 15 lines of glue.

## API

| Symbol | Purpose |
|---|---|
| `AvaDevice(url, cid, token).reading/.event/.push` | push client (token + retries) |
| `Tool(name, description, run, schema, required)` | one pull capability |
| `serve_pull(tools, host, port, block=True)` | run the `/tools`+`/call`+`/health` facade |
| `SerialRelay(port, baud).tools()/.call()/.pump(ava)` | bridge a USB board |

`Tool` has no `access` field, so the `serve_pull` facade reports no consent
tier. That is fine for a `role: device` connector — Ava classifies undiscovered
device tools as `physical` — but declare a `"*": physical` (or `write`)
`dynamic_access` catch-all in the manifest rather than relying on a tier the
facade never sends. It also means putting [`ava_mcp`](ava_mcp/README.md) in
front of a `serve_pull` facade has no tiers to carry through.
