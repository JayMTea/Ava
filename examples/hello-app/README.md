# Hello App: an example Ava connector

A minimal, self-contained third-party app that shows the whole integration
contract: a **left-rail tile**, an **embedded UI**, a **health probe**, and live
**agent tools**, all from one `connector.yaml` plus a small web server, with **no
changes to Ava's code**.

## Run it (the zero-source-edit acceptance test)

```bash
# 1. Start the app's own web server (its UI + /health + /tools + /call)
python examples/hello-app/server.py        # serves http://127.0.0.1:8477

# 2. Register it with Ava by dropping the folder into your data root
mkdir -p "${AVA_HOME:-$PWD}/connectors"          # ava setup does not create this
cp -r examples/hello-app "$AVA_HOME/connectors/hello"

# 3. Restart Ava (or `ava up`) and open the web app
#    -> "Hello App" now appears in the left rail
#    -> clicking it renders the app's page, embedded same-origin
#    -> ask Ava: "ping the hello app"  (its tools were discovered live)
```

That's it. You edited zero lines of Ava.

## What each piece does

| File | Role |
|---|---|
| `connector.yaml` | The manifest. `ui.embed: iframe` + `url` gives the rail tile and the embedded UI (proxied same-origin under `/apps/hello/`). `service.probe` gives the dashboard health row. `actions.discover` bridges the app's tool set to Ava. `egress` is auto-rendered into the agent's network policy. |
| `server.py` | The app. Stdlib-only HTTP server exposing `/` (UI), `/health`, `/tools`, `/call`. Replace this with your real app. |

## The contract your app implements

- `GET /health` → `{"ok": true}`, for the dashboard service matrix.
- `GET /` (plus any UI routes) → your web UI. Read `?theme=light|dark` to match Ava.
- **Agent tools**, either:
  - **discovered**: `GET /tools` → `{"tools":[{name,description,inputSchema}]}` and
    `POST /call {name, arguments}` → `{"text": "..."}` (what this example does), or
  - **declared**: list them under `actions.static` in the manifest with a
    `method`/`path`, and Ava calls your REST endpoints directly.

See [../../docs/CONNECTOR_SDK.md](../../docs/CONNECTOR_SDK.md) for the full reference.
