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

### What `/healthz` carries, and why it is more than a health check

`/healthz` is the one route the shim's auth middleware skips, and the one the
bridge already polls every 15s. So it is where anything the bridge needs
*cheaply and often* rides along, rather than earning a route of its own:

| Field | Read by | Meaning |
|---|---|---|
| `ready` | `RemoteRuntime.available()` | the sandbox exists and the CLI resolves |
| `authed` | `available()` | present only when a token was offered; `false` means the two containers disagree on `AVA_AGENT_TOKEN` |
| `capabilities` | `capabilities()` | what this container supports; the bridge **fails closed** on anything not listed |
| `model` / `provider` | `sandbox_info()` → `models.effective_brain()` | which model the sandbox is running |

`model` and `provider` come from the NemoClaw registry
(`~/.nemoclaw/sandboxes.json`), not from `nemoclaw list --json` — a health probe
must not shell out, and the cached-CLI path answers `None` to its first caller,
which would make the brain appear only on the second poll.

The keys are **omitted, never blanked**, when the registry cannot answer, and
`health.model` in `capabilities` is what separates the two silences: advertised
means the shim looked and the sandbox has no model onboarded; absent means the
container predates the field and cannot answer at all. Those are fixed on
different machines, so Setup reports them as different sentences.

This table is a contract across two files, and it was broken once in exactly the
way a table prevents: `RemoteRuntime.available()` read `model` and `provider`
that `healthz()` never sent, so every `remote` install resolved an empty brain
and told its owner no model was configured while the sandbox answered turns.
`tests/test_remote_brain_contract.py` now holds both halves at once — it drives
the real shim app with a real `RemoteRuntime` and fails if the bridge reads a
field the shim does not send.

### Networking note (host-dependent)

The sandbox is created on the *host* Docker daemon (a sibling of the compose
containers), so it must be able to route to the bridge. The entrypoint maps
`host.openshell.internal` to the bridge container's IP (autodetected, or set
`AVA_BRIDGE_IP`). Depending on your Docker network setup this is the one piece
that may need adjustment. Validate that a chat turn actually calls a tool after
first bring-up.

## How Ava wraps OpenClaw without forking it

Ava vendors no OpenClaw source. It talks to a running OpenClaw the way any other
operator client does — over its gateway — and the whole relationship is one
Python file (`ava_bridge/runtime/openclaw_gw.py`) plus one TypeScript file
(`frontend/src/lib/agentApi.ts`). A guard fails the build if a gateway method is
called from anywhere else, so an upstream rename lands in one adapter instead of
across the app.

### Captured from life, not from docs

Every dead surface this wrapper has ever shipped came from the same mistake:
**code written from prose documentation, agreed with by a test fake built on the
same assumption.** A fake that shares the caller's belief agrees with the
caller. Eight invented method names, one invented event topic, a dead iframe
route and three mechanisms that had never once worked all passed the full test
suite before failing in a browser. Nothing but a live probe ever caught one.

So the contract is captured, not written:

```bash
.venv/bin/python qa/capture_gateway.py --schemas   # learn from a live gateway
.venv/bin/python qa/capture_gateway.py --check     # re-probe, diff, exit 3 on drift
```

It learns each method's schema from the gateway's **own `INVALID_REQUEST`
messages** — a deliberately wrong call executes nothing and is the cheapest safe
probe — and writes `qa/fakes/gateway-schemas.json`: 44 method schemas, the event
vocabulary, the transcript shapes, the abort and approval shapes. Each entry is
stamped with the build it came from, and the file records what could **not** be
captured and why, because silence about a gap reads as "fully captured".

### What holds it together

| Guard | Fails when |
|---|---|
| `test_gateway_capture_consistency` | the fake stops agreeing with the capture, or a hand-written schema contradicts it |
| `test_gateway_method_names` | a method or subscribed topic is named that the gateway does not have, or a gateway call appears outside the seam |
| `test_no_runtime_name_dispatch` | code picks a runtime by name, compares `rt.name` to a literal, or reaches into `rt._client` |
| `test_nemoclaw_layout` | vendor paths or hostnames are spelled outside the runtime package |
| `test_run_event_vocabulary` + `chatEvents.contract.test.ts` | Ava's own run-event kinds stop matching between Python and TypeScript |
| `qa/test_23_live_gateway_turn` | a whole turn stops working through a real bridge process |

The rule this encodes, for anyone extending the wrapper: **if you cannot point
at where a shape was captured, do not ship code that depends on it.** Add the
probe first. The gateway will tell you what it wants — it is the only thing that
reliably does.

### After an OpenClaw upgrade

Re-run the capture and read the diff. A changed schema is a real change to the
contract, and the guards above will tell you which surfaces depend on it before
your users do.

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
