# Connectors

> This is the in-repo note for the `connectors/` folder. The full, canonical
> connector contract (manifest fields, embed tiers, agent tools, egress, devices)
> lives in [docs/CONNECTOR_SDK.md](../docs/CONNECTOR_SDK.md).

A **connector** teaches Ava about one thing she monitors or drives — a service to
health-check, a performance log to chart, an egress policy, and agent actions
(or a whole MCP server). Ava's **dashboard service matrix**, **performance
aggregator**, **agent tools**, and **egress policy** are all *derived* from these
manifests, so adding your app is dropping a folder here — no core-code changes.
The easiest path is the **Setup → Connect an app** GUI, which writes the manifest
for you; this file documents the format underneath it.

## Where they load from
- **built-in:** `connectors/<id>/connector.yaml` (this folder — the first-party ones)
- **yours:** `$AVA_HOME/connectors/<id>/connector.yaml` (overrides built-ins by id)

## Manifest format
```yaml
id: myapp
label: My App
kind: app                 # core | inference | media | app
enabled: true
service:                  # shows in the dashboard Service health matrix
  name: My App
  probe: "http://127.0.0.1:9000/health"   # optional HTTP health check
  unit: myapp.service                       # optional systemd user unit
perf:                     # optional — if the app writes an Ava performance.jsonl
  app: myapp
  path: "${AVA_HOME}/connectors/myapp/performance.jsonl"
egress:                   # optional — what Ava's agent tools may reach
  hosts:  ["127.0.0.1:9000"]            # host:port endpoints (auto-derived from actions too)
actions:                  # optional — each becomes an agent tool (+ egress rule)
  - id: myapp_do
    method: POST                        # GET args -> query params; POST -> JSON body
    path: "/api/do"                     # forwarded to base_url + path
    description: "what it does"
    # confirm: true                     # require your approval before Ava runs it
base_url: "http://127.0.0.1:9000"       # where `actions` are sent (also probe origin)
# --- or wrap an MCP server instead of declaring actions: ---
# mcp:
#   command: ["npx", "-y", "@modelcontextprotocol/server-github"]
#   sandbox: docker                     # run the server contained (recommended)
#   env: { GITHUB_TOKEN: "${GITHUB_TOKEN}" }
```

See **[docs/CONNECTOR_SDK.md](../docs/CONNECTOR_SDK.md)** for the full field
reference (MCP servers, container isolation, discovery, per-action approval,
UI tiles, chat pickups).

Path variables you can use: `${AVA_HOME}` `${AVA_LOGS}` `${AVA_DATA}` `${ROOT}`.

## Generating egress policies
A connector's `egress` block renders into an OpenClaw egress policy (the sandbox's
allow-list) — so declaring what an app needs is enough:
```bash
ava connector policies            # preview generated policies for all connectors
ava connector policies myapp --write   # write agent/policies/generated/myapp.yaml
```
Then `cd agent && ./install.sh` deploys the policies into the sandbox.

## Add one
```bash
ava connector new myapp     # scaffolds $AVA_HOME/connectors/myapp/connector.yaml
ava connector list          # see everything currently loaded
```
Then restart Ava (or `ava up`). Your app appears in the dashboard's **Service
health matrix** automatically, and any `perf.path` you set feeds the **Vitals**
charts.
