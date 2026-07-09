# AvaClient (Arduino)

Wire **any** board to your self-hosted [Ava](../../../README.md) assistant. Push
sensor readings and events, and answer Ava's on-demand reads and commands — in a
few lines, with **no external libraries and no hardcoded boards**.

AvaClient speaks the two contracts Ava already uses for device connectors (see
[`docs/DEVICE_CONNECTORS.md`](../../../docs/DEVICE_CONNECTORS.md)); it does not
add anything to Ava's core.

## Your device, your firmware

AvaClient is an **optional convenience library, provided as-is**. **You own your
device and its firmware** — writing it, flashing it, testing it on real hardware,
and keeping it working. Ava's responsibility ends at the documented HTTP contract
(the push and pull endpoints); Ava does **not** build, flash, test, or maintain
device firmware, and ships no board-specific code. Use this library if it helps,
or adapt/replace it freely — anything that honors the contract works.

## Install

- **Arduino IDE:** Sketch → Include Library → Add .ZIP Library… → select this
  `AvaClient` folder (or copy it into your `libraries/` directory).
- **PlatformIO:** add the path to `lib_deps`, or drop it in `lib/`.

## Board-agnostic by design

You bring the transport; AvaClient handles the protocol.

- **Push** uses any Arduino `Client` you pass to `setPushClient()` — `WiFiClient`,
  `EthernetClient`, a cellular client, anything.
- **Pull** is answered over either an HTTP `Client` (`handleHttp`) for networked
  boards, or any `Stream` (`handleStream`) for USB/serial boards relayed by the
  host adapter.

## Quick start (push)

```cpp
#include <WiFi.h>            // your board's networking
#include <AvaClient.h>

WiFiClient net;
AvaClient ava;

void setup() {
  WiFi.begin(SSID, PASS);
  while (WiFi.status() != WL_CONNECTED) delay(500);
  ava.begin("192.168.1.50", 8096, "greenhouse", "TOKEN_FROM_ava_device_token");
  ava.setPushClient(net);
}
void loop() {
  ava.reading("temperature", 21.5, "C");
  delay(30000);
}
```

Get the token with `ava device token <connector-id>` (or, once Phase 1 lands, the
"Show device push token" button in Setup → Connectors).

## Add pull (Ava reads/commands the device)

```cpp
double readTemp() { return 21.5; }
String setRelay(const String &args) {
  bool on = AvaClient::argBool(args, "on");
  return on ? "Relay ON" : "Relay OFF";
}

ava.addSensor("read_temperature", "C", "Read the temperature", readTemp);
ava.addCommand("set_relay", "Turn the relay on/off", setRelay);

// networked board:
WiFiClient c = server.available(); if (c) ava.handleHttp(c);
// or USB board (relayed by sdk/host serial_bridge.py):
ava.handleStream(Serial);
```

Then point the connector's `actions.discover.base` at the board (networked) or at
the host adapter (serial), and deploy its egress policy.

## Examples

- `PushReadings` — networked board, push only.
- `PushAndPull` — networked board, push + HTTP pull.
- `SerialBridge` — USB board, pull over serial + emit readings (needs the host
  adapter in [`sdk/host`](../../host)).

## API

| Method | Purpose |
|---|---|
| `begin(host, port, cid, token, tls=false)` | configure target + identity |
| `setPushClient(Client&)` | the client used to POST pushes |
| `reading(name, value, unit=nullptr)` | push a reading |
| `event(name, message, severity, notify)` | push an event |
| `emitReading/emitEvent(Stream&, ...)` | push over serial (host relays it) |
| `addSensor(name, unit, desc, fn)` | register a read (pull) |
| `addCommand(name, desc, fn)` | register a command (pull) |
| `handleHttp(Client&)` | answer one HTTP pull request |
| `handleStream(Stream&)` | answer one serial pull request |
| `argStr/argNum/argBool(json, key)` | parse command arguments |

Tool capacity is `AVA_MAX_TOOLS` (default 12); `#define` it before the include to
change it.
