# Example connectors

Six manifests you can copy into your data root. Nothing here needs a change to
Ava's code — copy the folder, restart, and the app is wired in.

```bash
mkdir -p "${AVA_HOME:-$PWD}/connectors"     # ava setup creates this now; older homes may not have it
cp -r examples/<name> "${AVA_HOME:-$PWD}/connectors/<id>"
```

| Folder | Port | What it is here to show |
|---|---|---|
| [`hello-app`](hello-app/) | 8477 | The whole loop, minimally: health row, embedded UI, live-discovered tools. Start here. |
| [`device-app`](device-app/) | 8479 | `role: device` and **push** ingest — the app hands Ava events when *it* decides. |
| [`home-assistant`](home-assistant/) | — | A real third-party MCP integration over the legacy HTTP+SSE transport. |
| [`stridewell`](stridewell/) | 8481 | Health data over real MCP. Reads are `sensitive`, not `read`. |
| [`ledgerline`](ledgerline/) | 8482 | Finance, **read-only by design**, with `confirm:` on the one tool that exports. |
| [`hearthwire`](hearthwire/) | 8483 | Home control — the only example declaring `physical`, plus `confirm:` on a door lock. |

Each folder has its own README. The ports differ so you can run them side by side.

## Which one answers your question

- **"How do I get anything working?"** — `hello-app`.
- **"How do I expose my own app's tools?"** — `stridewell`, which speaks MCP directly
  rather than Ava's `ava-tools/1` facade, so it stays useful to any MCP client.
- **"How do I stop the assistant doing something irreversible?"** — `hearthwire`.
  `physical` is the one tier [`_infer_access`](../ava_bridge/connectors.py) will never
  derive on its own; it has to be declared, and once declared it cannot be granted away.
- **"How do I keep a whole app read-only?"** — `ledgerline`.

The consent tiers these demonstrate are documented in
[docs/CONNECTOR_SDK.md](../docs/CONNECTOR_SDK.md).
