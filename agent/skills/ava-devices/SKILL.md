---
name: "ava-devices"
icon: gauge
description: "How Ava reads and reports on the user's own connected devices and sensors. Use whenever the user asks about their hardware/sensors — a reading (temperature, humidity, motion, door, light, power), what a device is doing, whether anything happened, the latest event, or to control a device (turn on/off, set, toggle). Trigger keywords - sensor, device, Arduino, Nicla, Portenta, ESP32, board, greenhouse, garage, motion, temperature reading, did anything happen, latest reading, turn on, turn off, toggle, relay, pump, light, smart home."
---

# Answering Device & Sensor Questions

The user can wire their own hardware/sensor apps to Ava (Arduino, Nicla, Portenta,
ESP32, smart-home hubs, etc.). Ava reaches them two ways — **do not guess a reading
or say you have no access; call the tools.**

## 1. Reading what devices reported (push)

Device apps push events to Ava — readings and events like "motion detected". To
answer *"did anything happen?"*, *"what's the latest from the greenhouse?"*, *"any
alerts from my sensors?"*, call the **`device_events`** tool directly.

| Argument    | Required | Notes                                                         |
| ----------- | -------- | ------------------------------------------------------------- |
| `connector` | no       | A device id to filter to one app (omit for all devices).     |
| `limit`     | no       | Max events, newest first (default 50).                       |

- "Did anything happen with my devices?" → `device_events({})`
- "Latest from the greenhouse?" → `device_events({ "connector": "greenhouse" })`

Answer naturally from the returned events (name, value/unit, message, time).

## 2. Reading & controlling devices on demand (pull)

Each connected device app also exposes its own live tools (e.g.
`read_temperature`, `set_relay`, `greenhouse_...`). When the user asks for a
current reading or to control something ("turn on the pump", "is the door open"),
call that device's tool **directly** as a native tool call. These tools are
discovered from the user's app, so their exact names depend on what they built —
use the ones available to you.

## Do not

- Do not fabricate a sensor value — read it with the appropriate tool.
- Do not claim the sandbox blocks network/hardware access; these tools reach the
  user's device apps through an approved path and work reliably.
- Do not send a device command you're unsure about without confirming, if it looks
  physically consequential (unlocking, heating, high power).
