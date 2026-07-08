# Design sketch — optional "warm agent" mode (future)

**Status:** design only, not implemented. Captures a packageable way to remove the
per-turn agent cold-start without coupling Ava to any one machine or operator.

## Problem

Every conversational turn runs through `AgentRuntime.run_turn`. On the default
runtime (NemoClaw), each turn currently boots an **embedded** agent: it spawns the
7 MCP tool-server node processes, loads plugins, and compiles the ~33k-token
system context from scratch, then tears it all down. Measured on a unified-memory GPU box:

- a 1-token reply ("OK") turn ≈ **5–9s wall-clock**; the model itself is ~2s.
- so ~5–8s per turn is fixed boot overhead, independent of the question.

This is *by design today* — `agent/install.sh` notes "the agent runs embedded, so
a full [gateway] recover isn't required." Embedded is what makes a fresh fork work
with zero pairing/gateway setup. The cost is latency.

> Not the same as the model latency work already shipped (reasoning budget in
> `ava_router.py`). That reduced *generation* tokens; this targets *boot* overhead.

## Goal

An **optional**, config-gated warm mode that keeps the agent + its MCP servers
resident between turns, so `run_turn` dispatches to an already-warm worker instead
of cold-booting. Target: ~5–8s → <1s of overhead per turn.

## Hard requirements (why this stays packageable)

1. **Off by default.** `agent.warm: false`. A bare `git clone` + `nemoclaw onboard`
   + `agent/install.sh` fork MUST keep working with **zero** extra setup, on the
   embedded path. Warm mode is an opt-in optimization, never a prerequisite.
2. **No per-operator state in the repo.** Device ids, gateway tokens, and scopes
   are generated per install and live only under the runtime sandbox / `AVA_HOME`
   — never committed. Nothing about warm mode may hardcode a machine, path, IP,
   location, or account. (Config over hardcode; see PACKAGING_PLAN §1, §5.4.)
3. **Self-provisioning, not manual.** If warm mode needs setup (e.g. granting the
   agent device gateway scopes), that setup is done **idempotently inside
   `AgentRuntime.provision()`** — never a hand-run `openclaw devices …` command.
   A forker types `agent.warm: true` and `ava agent provision`; nothing else.
4. **Graceful fallback.** If the warm path is unavailable or fails mid-turn, the
   runtime falls back to embedded and the turn still completes. Warm mode can only
   make things faster, never break a turn.

## Where it plugs in (`AgentRuntime`)

The seam already exists (`ava_bridge/runtime/base.py`): `warm()`, `run_turn()`,
`provision()`, `status()`. Warm mode is expressed through them, so it works for
NemoClaw today and any future runtime adapter.

```python
class AgentRuntime(ABC):
    supports_warm: bool = False          # NEW capability flag

    def warm(self) -> None: ...           # already exists — make it establish/refresh the warm worker
    def run_turn(self, ...): ...          # dispatch to warm worker when live, else embedded
    def provision(self, ...): ...         # idempotently set up whatever warm mode needs
    def status(self) -> dict: ...         # report {"warm": {"enabled", "live", "backing"}}
```

Config (all under `agent.*`, resolved via `ava_bridge/settings.py`, env-overridable):

```yaml
agent:
  warm: false            # [AVA_AGENT_WARM] opt-in; embedded stays the default
  warm_backing: auto     # auto | gateway | pool   (how warm mode is realized)
  warm_idle_ttl: 900     # seconds a warm worker may sit idle before it's reaped
```

## Two implementation options (pick per runtime; keep the seam identical)

### A. Gateway-backed (NemoClaw-specific)
openclaw already ships a persistent **Gateway daemon**; `openclaw agent` is meant
to run *through* it (reusing its loaded MCP servers + provider) instead of
embedded. Today it falls back to embedded because the agent device is paired with
only `operator.pairing` scope and can't get `operator.read/write`.

- **Make it automatic:** `NemoClawRuntime.provision()` grants the agent device the
  needed scopes idempotently (the gateway equivalent of `devices rotate … --token
  $OPENCLAW_GATEWAY_TOKEN`), reading the token from the runtime env — no operator
  action, nothing committed.
- **Verify warm reuse:** confirm the gateway keeps the 7 MCP servers resident
  across turns (unconfirmed today). If it re-spawns them per turn, this option's
  win shrinks and Option B is preferred.
- **Risk:** couples to openclaw gateway/device internals the packaging plan wants
  abstracted. Acceptable only because it lives entirely inside the NemoClaw
  adapter, behind the interface.

### B. Persistent worker pool (runtime-agnostic — preferred long-term)
The adapter owns a small pool of long-lived agent workers that hold MCP servers +
context loaded; `run_turn` checks out a warm worker, runs the turn, checks it back
in. Idle workers reaped after `warm_idle_ttl`.

- Doesn't depend on any specific host's gateway/pairing — works for any future
  `AgentRuntime`, which is the direction §5.4 wants.
- More code in the adapter (lifecycle, health, restart-on-crash, per-session
  affinity so a session's memory lands on a consistent worker).

`warm_backing: auto` = use the gateway if the runtime exposes a healthy one, else
the pool, else embedded.

## Acceptance criteria (how we'd validate, decoupled)

- Fresh clone, `agent.warm` unset → identical behavior to today (embedded), no new
  required steps. **This is the decoupling test.**
- `agent.warm: true` + `ava agent provision` on a clean box → warm path comes up
  with no hand-run credential/gateway commands.
- Debug trace shows turns reuse the warm worker (no `EMBEDDED FALLBACK`); MCP
  server count stays steady across turns instead of spiking per turn.
- Kill the warm worker mid-session → next turn still answers (fallback), then
  re-warms.
- Grep of the repo for machine/operator specifics (paths, IPs, device ids, tokens,
  location) stays clean.

## Not doing now / open questions

- Whether NemoClaw's gateway actually keeps MCP warm (decides A vs B).
- Multi-session affinity + memory consistency for Option B.
- Interaction with ghost mode (`discard_session`) and the live chain-of-thought
  poller (`ava_bridge/turns.py`), which today tails a per-session sandbox file.
