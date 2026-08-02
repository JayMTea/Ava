# Example connectors

Three manifests you can copy into your data root. Nothing here needs a change to
Ava's code — copy the folder, restart, and the app is wired in.

```bash
mkdir -p "${AVA_HOME:-$PWD}/connectors"     # ava setup creates this now; older homes may not have it
cp -r examples/<name> "${AVA_HOME:-$PWD}/connectors/<id>"
```

| Folder | Port | What it is here to show |
|---|---|---|
| [`hello-app`](hello-app/) | 8477 | The whole loop, minimally: health row, embedded UI, live-discovered tools. Start here. |
| [`device-app`](device-app/) | 8479 | `role: device` and **push** ingest — the app hands Ava events when *it* decides. |
| [`home-assistant`](home-assistant/) | — | A real third-party MCP integration over the legacy HTTP+SSE transport, with every actuating tool pinned to `physical`. |

Each folder has its own README. The ports differ so you can run them side by side.

## Which one answers your question

- **"How do I get anything working?"** — `hello-app`.
- **"How do I expose my own app's tools?"** — `hello-app`, whose `actions.discover`
  bridges the app's whole tool set live, so every tool it grows appears with no
  per-tool wiring. For the static alternative — one `actions:` entry per endpoint
  with an explicit `access:` tier — read
  [`connectors/_template/connector.yaml`](../connectors/_template/connector.yaml),
  which annotates every field.
- **"How do I connect something that already speaks MCP?"** — `home-assistant`.
  The whole integration is one manifest and two env vars, and it ships inert
  until you configure them.
- **"How do I stop the assistant doing something irreversible?"** — `home-assistant`,
  whose `dynamic_access` pins `"*": physical` so anything that actuates asks every
  time. `physical` is the one tier [`_infer_access`](../ava_bridge/connectors.py) will
  never derive on its own; it has to be declared, and once declared it cannot be
  granted away.

The consent tiers these demonstrate are documented in
[docs/CONNECTOR_SDK.md](../docs/CONNECTOR_SDK.md).
