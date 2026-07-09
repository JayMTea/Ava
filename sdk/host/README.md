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
