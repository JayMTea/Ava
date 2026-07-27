# The agent configuration kit (`agent/`)

This directory declares everything Ava-the-agent gets when it is provisioned
into its sandboxed runtime (NemoClaw/OpenClaw; see
[docs/AGENT_RUNTIME.md](../../docs/AGENT_RUNTIME.md)):

- **`mcp_server_*/`**: zero-dependency Node MCP tool servers, one per
  capability group (content, productivity, system, admin, wellness, …). Each
  tool is a single `.mjs` module; the `_server.mjs` harness auto-loads every
  module in its folder. Tools reach the host bridge at `/internal/*` with
  HMAC-derived, per-group scoped tokens.
- **`skills/`**: native skills (structured prompts plus tool guidance) deployed
  alongside the tools.
- **`policies/`**: deny-by-default egress policies. Hand-authored ones live
  here; per-connector policies are *generated* into `policies/generated/` by
  `ava connector policies <id> --write` (gitignored, derived).
- **`templates/` + `render_persona.py`**: the persona is rendered from
  `ava.yaml` (`brand.*`, `owner.*`) so nothing personal ships in the repo.
- **`install.sh`**: idempotent deploy of all of the above into the sandbox.
  It also picks up an optional gitignored `/overlay/agent/` with private
  servers, skills, and policies; that is how a personal deployment extends the
  kit without touching tracked files.

Two planes, in one sentence: the **bridge** (`ava_bridge/`, the web app on
:8096) is the experience plane; the **agent** (this kit, running inside the
sandbox) is the capability plane; and they only talk over enumerated,
token-gated `/internal/*` routes and the inference router.

## The system, in three diagrams

Rendered by `arch.py` from the live architecture manifest and drift-checked
against the running code (services, MCP tools, egress policies), so they show
the system as it actually is, not as it was once drawn.

**Security**: trust zones and gates, from the internet down to the sandbox.

[![Security diagram](diagrams/security.svg)](diagrams/security.svg)

**System** and **Network** render this install's *actual* topology — device
labels and whichever private sibling apps are connected — so they are generated
locally and gitignored rather than committed. Run `python agent/docs/arch.py render`
to produce `diagrams/system.svg` and `diagrams/network.svg` for your own deployment.

For the app-agnostic picture of how Ava is put together, see
[docs/assets/architecture.svg](../../docs/assets/architecture.svg), which is
hand-authored and tracked.

The SSOT manifest (`architecture.yaml`) and the `.d2` sources stay
**deployment-specific and gitignored**; the rendered SVGs above are committed
as the reference topology. `arch.py` (the sync/check/render pipeline) skips
gracefully when the manifest is absent. See also the root
[README.md](../../README.md) and
[docs/CONNECTOR_SDK.md](../../docs/CONNECTOR_SDK.md).
