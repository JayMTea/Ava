# Connect your devices

Wire your own hardware into Ava: an Arduino, an ESP32, a smart-home hub, any
sensor. Once connected, you can ask about it in plain language (*"what's the
greenhouse temperature?"*), command it (*"turn on the pump"*), and get dashboard
alerts when the device decides something matters (motion, a threshold crossed).

Ava stays hardware-agnostic by talking to a small app **you** run next to the
device, not to the device itself; no device protocols are baked into Ava. The
[example device app](../examples/device-app/) is a ready starting point: about
150 lines of stdlib Python you copy and point at your real device I/O.

---

## Reading and commanding devices (pull)

Your device app exposes two tiny endpoints (`GET /tools`, `POST /call`), and Ava
wires it in exactly like any other app: **Setup → Connectors → Connect an app**,
paste its address, click **Detect**, click **Connect app**. The guided flow,
with screenshots and a video, is [Connect your apps](CONNECT_YOUR_APPS.md); it
works the same for device apps.

## Getting notified (push)

For events your device decides to send (motion, a reading, a threshold), give
your app the connector's push token and it can hand Ava events at any time:

```bash
ava device new greenhouse        # scaffold a device connector (if you didn't use the browser)
ava device token greenhouse      # print the token your app presents when pushing
ava device list                  # each device connector and its directions (pull,push)
ava device events greenhouse     # watch events arrive, live
```

Pushed events are stored (they survive restarts), surface live on the dashboard,
raise an alert when marked `notify` or warn/critical, and are readable by the
agent, so *"did anything happen with my devices?"* just works.

## Try it end to end

The example device app demonstrates both directions in about a minute; the full
run-through is in [Device connectors: worked example](DEVICE_CONNECTORS.md#worked-example-the-demo-device-app).

## Going further

- Full contract (manifest fields, the push event format, authentication):
  [Device connectors](DEVICE_CONNECTORS.md)
- By design, Ava ships no serial/BLE/MQTT/Zigbee/Matter transports and no rules
  engine; your app owns the device I/O and decides when to notify.
