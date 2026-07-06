# Ava Agent Runtime

Ava is an **agent**, not just a chat box — she has tools, skills, sandboxed
execution, per-connector egress policies, and persistent memory. All of that is
provided by an **agent runtime**. Ava talks to it through one interface
([`ava_bridge/runtime/`](../ava_bridge/runtime/)), so the runtime is pluggable and
its specifics live in a single adapter.

## The default runtime: NemoClaw (recommended)

[**NemoClaw**](https://github.com/NVIDIA/NemoClaw) (NVIDIA, Apache-2.0) is the
default and recommended runtime. It's a reference stack that runs
[OpenClaw](https://openclaw.ai) inside an [OpenShell](https://github.com/NVIDIA/OpenShell)
sandbox. That gives Ava:

- **Tools & connectors** — every capability (`run_gpu_job`, web, your
  connector apps) executes inside the sandbox.
- **Security isolation + egress policies** — each tool group can reach only the
  endpoints it declares ([agent/policies/](../agent/policies/)); the sandbox is
  Ava's network/filesystem boundary. This is what makes the connector SDK's
  auto-generated egress policies actually mean something.
- **Persistent, per-conversation memory** — one session-id = continuous memory.
- **Skills + self-improvement** — the `ava-*` skills and self-coding loop.
- **Live chain-of-thought** — the UI streams Ava's real reasoning/tool steps.

**It is hardware-portable.** OpenShell creates the sandbox from a container image
via the local Docker daemon (a community `openclaw` image, or your own
Dockerfile) — so it runs wherever Docker + the runtime run, not only on a
specific GPU/box.

## Set it up

```bash
ava agent provision --install     # installs the nemoclaw CLI, then guides you
nemoclaw onboard                  # (interactive) configure inference + create the sandbox
ava agent provision               # deploy Ava's tools/policies/skills into it
ava agent status                  # verify: CLI, sandbox, active runtime, health
```

`ava agent provision` is idempotent — re-run it any time (and after
`nemoclaw <name> rebuild`).

## Making it required (the "MUST")

By default Ava degrades gracefully if the runtime is missing (see below). To make
the full runtime **mandatory** — so the app fails loud instead of silently
serving tool-less chat — set in `ava.yaml`:

```yaml
agent:
  required: true
```

Then a missing runtime is a hard, actionable error at startup, in `ava doctor`,
and on every chat turn (with the exact fix), rather than a quiet downgrade.

## The fallback: Direct (tool-less) chat

When no runtime is present **and** `required` is false, Ava talks to the inference
endpoint directly — a working but **tool-less** assistant (no tools, no sandbox,
no agent memory; it replays recent history for continuity). This is the graceful
floor so a fresh install / unsupported box isn't a dead end — the on-ramp, not
the destination. Force it explicitly with `agent.enabled: false` or
`agent.runtime: direct`.

| | NemoClaw (full) | Direct (floor) |
|---|:--:|:--:|
| Tools / connectors / images | ✅ | ❌ |
| Sandboxed + egress-policed | ✅ | ❌ |
| Persistent agent memory | ✅ | replayed history |
| Live chain-of-thought | ✅ | ❌ |
| Works with zero setup | needs provisioning | ✅ |

## Adding another runtime

Implement [`AgentRuntime`](../ava_bridge/runtime/base.py) (`run_turn`, `exec`,
`provision`, `status`, …) in `ava_bridge/runtime/<name>.py`, register it in
`runtime/__init__.py`, and select it with `agent.runtime: <name>`. Ava's core
never changes — it only talks to the interface.
