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

> **Installing the CLI:** NemoClaw is installed by NVIDIA's official installer
> (`curl -fsSL https://raw.githubusercontent.com/NVIDIA/NemoClaw/main/install.sh | bash`)
> — **not** `npm install -g nemoclaw` (that package is an empty stub). It needs
> **Node ≥ 22.16** and a reachable Docker daemon. `ava agent provision --install`
> runs this for you.

`ava agent provision` is idempotent — re-run it any time (and after
`nemoclaw <name> rebuild`).

**Inference endpoint for `nemoclaw onboard`**: point the agent at Ava's router,
`http://host.openshell.internal:8010/v1` — the router starts inside `ava up`
automatically (embedded), fronts whatever backends `ava.yaml` declares, and
gives the agent failover + perf logging for free. If you expose the router
beyond loopback (`inference.router.host: 0.0.0.0`), also configure the bearer
token from `$AVA_HOME/secrets/router_token`.

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
| Tools / connectors / images | Yes | No |
| Sandboxed + egress-policed | Yes | No |
| Persistent agent memory | Yes | replayed history |
| Live chain-of-thought | Yes | No |
| Works with zero setup | needs provisioning | Yes |

## Full agent in Docker (the `remote` runtime)

By default the Docker image runs the **tool-less** Direct floor — NemoClaw needs
a Docker daemon to spawn its sandbox, which the bridge container doesn't have.
The `remote` runtime closes that gap without putting Docker-in-Docker in the
bridge: a dedicated **`agent`** container (Node + `nemoclaw` + the host Docker
socket) runs the sandbox and exposes it over HTTP; the bridge talks to it via
`RemoteRuntime`, so tools, memory, and live chain-of-thought all work — the
bridge just doesn't care that the runtime is across the network.

[![The remote runtime: bridge, agent container, sandbox](assets/agent-remote-runtime.svg)](assets/agent-remote-runtime.svg)

Enable it:

```bash
# in deploy/.env
AVA_AGENT_ENABLED=1
AVA_AGENT_RUNTIME=remote
AVA_ROUTER_HOST=0.0.0.0        # so the sandbox can reach the router at ava:8010

docker compose --profile agent up -d     # or --profile full
```

The `agent` container's entrypoint waits for the bridge, then **onboards the
sandbox non-interactively** (`nemoclaw onboard --non-interactive`, configured
entirely from `NEMOCLAW_*` env — inference endpoint `ava:8010/v1`, provider
`compatible-endpoint`), maps `host.openshell.internal` to the bridge, runs
`agent/install.sh`, and serves the shim on `:9100` (auth-gated, never published).

> **Security:** the `agent` container mounts `/var/run/docker.sock`, which is
> **root-equivalent on the host**. That's why it's opt-in behind the `agent`
> profile and why Docker-in-Docker was kept out of the bridge. Run it only on a
> host you trust with that access.

**Networking note (host-dependent):** the sandbox is created on the *host* Docker
daemon (a sibling of the compose containers), so it must be able to route to the
bridge. The entrypoint maps `host.openshell.internal` → the bridge container's
IP (autodetected, or set `AVA_BRIDGE_IP`). Depending on your Docker network
setup this is the one piece that may need adjustment — validate a chat turn
actually calls a tool after first bring-up.

## Adding another runtime

Implement [`AgentRuntime`](../ava_bridge/runtime/base.py) (`run_turn`, `exec`,
`provision`, `status`, …) in `ava_bridge/runtime/<name>.py`, register it in
`runtime/__init__.py`, and select it with `agent.runtime: <name>`. `RemoteRuntime`
(the Docker full-agent path above) and `DirectRuntime` are worked examples. Ava's
core never changes — it only talks to the interface.
