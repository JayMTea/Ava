# mcp/adapters/

Per-server overrides applied when MCP tools are imported. One file per server, named
`<server>.yaml`. Use it to classify side effects honestly (unclassified MCP tools are treated
as `external_write`, so they need approval), tighten the descriptions the model sees, or set
per-tool timeouts.

```yaml
# mcp/adapters/docs.yaml
tools:
  read_text_file: {side_effects: none}
  list_directory: {side_effects: none, description: List files in the project knowledge base.}
  search_files:   {side_effects: none, timeout_s: 30}
```
