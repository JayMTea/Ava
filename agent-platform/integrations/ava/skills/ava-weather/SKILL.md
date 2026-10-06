---
name: "ava-weather"
icon: cloud
description: "How Ava answers anything about live weather, temperature, or forecasts by calling her get_weather tool. Use whenever the user asks about the weather, temperature, how hot or cold it is, rain, wind, humidity, conditions, the forecast, whether to bring a jacket or umbrella, or what it's like outside — for their home or anywhere else. Trigger keywords - weather, temperature, forecast, how hot, how cold, is it raining, will it rain, wind, humidity, sunny, cloudy, outside right now, jacket, umbrella, climate today."
---

# Answering Weather Questions

Ava has a real, working `get_weather` tool with live internet data. Any question
about current conditions or the forecast MUST be answered by calling it — never
guess, and never claim you can't reach the internet.

## When to use

Call `get_weather` whenever the user asks about:

- Current conditions or temperature ("what's the weather", "how hot is it").
- A forecast ("what's it like tomorrow", "this weekend", "next few days").
- Anything condition-driven ("do I need a jacket / umbrella", "is it raining").

## How to call it

Invoke `get_weather` DIRECTLY as a native tool call with its arguments — do not
write code, use `tool_search_code`, or describe the call.

| Argument   | Required | Notes                                                              |
| ---------- | -------- | ------------------------------------------------------------------ |
| `location` | no       | City/place name. Defaults to the user's configured home location if set. |
| `days`     | no       | Number of forecast days when the user asks ahead (e.g. 3 for "next few days"). |

Examples:

- "What's the weather?" → `get_weather({})` (uses the configured home location).
- "How's it looking in Tokyo?" → `get_weather({ "location": "Tokyo" })`.
- "Will it rain this weekend?" → `get_weather({ "days": 3 })`.

## After the tool returns

Answer naturally from the tool's result — temperature in °F, conditions, and any
relevant detail (wind, humidity, what to expect). Keep it warm and concise.

## Do not

- Do not answer a weather question without calling `get_weather` first.
- Do not say the sandbox blocks the network or that an operator must approve
  access — this tool already reaches its data source through an approved path and
  works reliably. The "deny-by-default" network notice does NOT apply to it.
