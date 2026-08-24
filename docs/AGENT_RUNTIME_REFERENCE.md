# Agent runtime reference

For operators and developers. This page holds the parts of the agent runtime you
do not need to get Ava working: how to make the runtime mandatory, what the
Docker `agent` container does on start, the one networking piece that depends on
your host, and how to plug in a runtime of your own.

Setting the runtime up is [step 3 of getting started](AGENT_RUNTIME.md), and that
page is where the Docker socket consent gate lives. Read it first.

## Making the runtime required

By default Ava degrades gracefully if the runtime is missing: chat still works,
without tools ([the Direct floor](AGENT_RUNTIME.md#the-fallback-direct-tool-less-chat)).
To make the full runtime **mandatory**, so the app fails loud instead of silently
serving tool-less chat, set in `ava.yaml`:

```yaml
agent:
  required: true
```

A missing runtime is then a hard, actionable error at startup, in `ava doctor`,
and on every chat turn (with the exact fix), rather than a quiet downgrade.

## What the `agent` container does on start

In the [`remote` runtime](AGENT_RUNTIME.md#full-agent-in-docker-the-remote-runtime),
the `agent` container's entrypoint:

1. Waits for the bridge.
2. **Onboards the sandbox non-interactively** (`nemoclaw onboard
   --non-interactive`), configured entirely from `NEMOCLAW_*` env: inference
   endpoint `ava:8010/v1`, provider `compatible-endpoint`.
3. Maps `host.openshell.internal` to the bridge.
4. Runs `agent/install.sh`.
5. Serves the runtime shim on `:9100` (auth-gated, never published).

### Networking note (host-dependent)

The sandbox is created on the *host* Docker daemon (a sibling of the compose
containers), so it must be able to route to the bridge. The entrypoint maps
`host.openshell.internal` to the bridge container's IP (autodetected, or set
`AVA_BRIDGE_IP`). Depending on your Docker network setup this is the one piece
that may need adjustment. Validate that a chat turn actually calls a tool after
first bring-up.

## Adding another runtime

Implement [`AgentRuntime`](../ava_bridge/runtime/base.py) (`run_turn`, `exec`,
`provision`, `status`, …) in `ava_bridge/runtime/<name>.py`, register it in
`runtime/__init__.py`, and select it with `agent.runtime: <name>`. `RemoteRuntime`
(the Docker full-agent path) and `DirectRuntime` are worked examples.

### What happened when that claim was tested

`OpenClawGatewayRuntime` was the fourth, and "Ava's core never changes" held
**for the call sites** and not for the interface. Worth recording honestly,
because it is the shape a fifth runtime should expect:

* No caller changed. `turns.py`, `provision.py`, `hub/agent.py` and the CLI all
  still talk to the same names.
* The **interface grew**, because the new runtime could do something none of the
  first three could: report a turn's progress instead of returning it finished.
  Adding a capability nobody had is exactly when an interface should grow.
* Every addition defaults to a **refusal**, not to a plausible empty success —
  `rpc_methods()` returns an empty set meaning *none*, `observe()` returns
  `None` meaning *no view of its own*. A new adapter inherits all of it and
  cannot accidentally claim a capability it lacks.
  `tests/test_runtime_capability_contract.py` pins that.

The members added:

| Member | Answers |
|---|---|
| `capabilities()` | what this container/adapter supports (promoted from `RemoteRuntime`) |
| `rpc_methods()` | what the runtime's control plane offers — empty means **none** |
| `rpc()` / `subscribe()` | one control-plane call; an ordered, bounded event queue |
| `supports_push_turns()` | whether `start_run()` works — `turns.py` branches on this, never on `isinstance` |
| `start_run()` / `iter_run()` | begin a turn; then yield its progress in **Ava's** four event kinds (`step` / `final` / `error` / `gap`), never the runtime's own |
| `observe()` | this runtime's own view of what is deployed, per scope, omitting what it cannot answer |

`iter_run()` is the one worth copying. Keeping the wire format behind the seam
is what stops the turn path learning any particular agent's event names — and a
rename upstream then lands in one adapter instead of in `turns.py`.

---

Back to [Set up the agent](AGENT_RUNTIME.md), or on to
[Connect your apps](CONNECT_YOUR_APPS.md).
