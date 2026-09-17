# Charts from your own application

Any authenticated connector tool can return an application-neutral recorded chart
or saved live chart. Ava validates and stores the payload, gives the agent a small
reference, and serves the chart through the owner's authenticated browser session.
It does not render model-generated HTML. Enable the existing `data_artifacts`
feature to capture charts. Consent and connector credentials apply before capture.

Put the payload in the MCP result's `_meta["ava/artifact"]`. Keep normal tool output
in `structuredContent`; Ava replaces artifact references with its own saved ID.
Existing `ava-artifact/1` Census/PUMS snapshots and `ava-artifact/2` Superset charts
remain supported through compatibility validators and retain their captions.

## Recorded values: ava-artifact/3

```json
{
  "schema_version": "ava-artifact/3",
  "id": "7284270f-d168-49ef-8e01-9729917e06af",
  "type": "analytics",
  "mode": "snapshot",
  "title": "Orders by department",
  "chart_type": "bar",
  "result": {
    "schema_version": "analysis-result/2",
    "id": "7284270f-d168-49ef-8e01-9729917e06af",
    "mode": "snapshot",
    "title": "Orders by department",
    "created_at": "2026-09-13T12:00:00Z",
    "unit": "orders",
    "dimension_label": "Department",
    "scope_label": "This week",
    "method": "Count of completed orders",
    "row_count": 2,
    "columns": [{"name": "label"}, {"name": "value"}],
    "rows": [{"label": "Sales", "value": 12}, {"label": "Support", "value": 8}],
    "citations": [{"title": "Method", "url": "https://app.example.org/method"}],
    "sources": [],
    "limitations": []
  }
}
```

Use `bar` or `table`. Snapshot and result IDs must be the same UUID. Each row has
a label and a finite numeric value or null for unavailable. No geography, Census
metric, release, local warehouse or personal app fields are required. The JSON
budget is 4 MiB, at most 5,000 rows and 100 column declarations. Source/citation
entries contain a URL and optional title. Limitations are displayed as text.
An optional inert SVG must identify this result and pass Ava's drawing allowlist;
scripts, external references and active elements are refused.

## Saved live charts

The operator first declares allowed app-relative destinations in its connector:

```yaml
x_artifacts:
  live_paths: [/charts, /reports/saved]
```

Then the tool may return:

```json
{
  "schema_version": "ava-artifact/3",
  "id": "7284270f-d168-49ef-8e01-9729917e06af",
  "type": "analytics",
  "mode": "live",
  "title": "Orders dashboard",
  "chart": {"citations": []},
  "visualization": {"format": "app", "path": "/charts/saved-42?period=week"}
}
```

The path must match a declared prefix on a path-segment boundary. Absolute URLs,
protocol-relative URLs, traversal (including encoded traversal), backslashes and
fragments are refused. The configured connector UI determines the app origin and
proxy; the tool cannot choose another origin. A live chart needs the connected app
to remain available. Recorded snapshots remain immutable and are stored in
`paths.data/analytics-artifacts.db`; disconnecting the connector removes access to
its artifacts until that connector is available again.

The store inventory and deletion helpers include recorded charts. The current
shell has no Data tab; use instance backup/inspection and artifact export routes.
`data.artifact_retention_days` defaults to zero (retain indefinitely); positive
retention prunes on store access. Backups preserve recorded charts independently
of live-store retention. See [Instance reference](INSTANCE_REFERENCE.md).
