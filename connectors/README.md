# Connectors

> This is the in-repo note for the `connectors/` folder. The full, canonical
> connector contract (manifest fields, embed tiers, agent tools, egress, devices)
> lives in [docs/CONNECTOR_SDK.md](../docs/CONNECTOR_SDK.md).

A **connector** teaches Ava about one thing she monitors or drives — a service to
health-check, a performance log to chart, and (on the roadmap) an egress policy
and agent actions. Ava's **dashboard service matrix** and **performance
aggregator** are *derived* from these manifests, so adding your app is dropping a
folder here — no core-code changes.

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
  routes: ["POST /internal/myapp/do"]   # bridge routes (host.openshell.internal:8096)
  hosts:  ["127.0.0.1:9000"]            # or direct host:port endpoints
actions:                  # optional — agent tools this connector exposes
  - { id: myapp_do, description: "what it does" }
```

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
