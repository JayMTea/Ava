# tools/ — native tools

`registry.yaml` declares every tool; each `<family>/__init__.py` implements one family.
Adding a tool: `docs/extending.md` → *Add a tool*.

## Contract

```python
def action(args: dict, ctx: ToolContext) -> str | dict | list:
    path = ctx.policy.resolve_path(args["path"], "read")   # confinement + data-access policy
    ctx.policy.check_url(url)                              # network policy (and every redirect)
    raise ToolError("what went wrong, for the model")      # expected failures
```

- Arguments arrive already validated against the tool's `input_schema`.
- Grants, approvals, timeouts, truncation, redaction and untrusted-content wrapping happen
  in `runtime/tool_executor.py` — do not reimplement them, and do not bypass them.
- `ctx.services` exposes platform services (memory, skills, tasks, sandbox) to built-ins.

| Family | Tools | Side effects |
|---|---|---|
| `filesystem` | `read`, `list`, `write` | none / local_write (writes only under `artifacts/` by policy) |
| `shell` | `run` | destructive — sandbox required, denied to all agents by default |
| `git` | `status`, `diff`, `log` | none (read-only; refs validated against option injection) |
| `browser` | `fetch` | read_external (allowlisted HTTPS hosts, SSRF-blocked) |
| `database` | `query` | none (read-only connection, one statement) |
| built-ins | `skill.load`, `memory.recall`, `memory.remember`, `task.delegate` | see `registry.yaml` |
