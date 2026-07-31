# ava_mcp — serve your app as a real MCP server

Your app already speaks the [`ava-tools/1` facade](../../../docs/CONNECTOR_SDK.md)
(`GET /tools` + `POST /call`). That's the easy way into Ava — but it's *Ava's*
protocol, so it wires you into Ava and nothing else.

This adapter puts genuine [Model Context Protocol](https://modelcontextprotocol.io)
in front of the same tools. One process, no code change, and your app is now
callable by Ava's `mcp:` connector block, Claude Desktop, an IDE — anything that
speaks MCP.

```
your app  ──ava-tools/1──▶  ava_mcp  ──MCP / JSON-RPC──▶  any MCP client
```

Stdlib only. No dependency on Ava.

## Install

Nothing to install — but `ava_mcp` is a plain package, not an installed
entry point, so `python -m ava_mcp` only resolves if the package's **parent**
directory is on `sys.path`. Any one of these works:

```bash
cp -r sdk/host/ava_mcp /path/to/your/app/     # copy it next to your code
PYTHONPATH=sdk/host python -m ava_mcp …       # or point PYTHONPATH at sdk/host
cd sdk/host && python -m ava_mcp …            # or just run from there
```

Run from the repo root without one of those and you get
`No module named ava_mcp`.

## Run it as a sidecar (no code change)

```bash
cd sdk/host          # or copy ava_mcp/ next to your app — see Install above
python -m ava_mcp --facade http://127.0.0.1:8097 --port 9310 \
                  --token-env MYAPP_TOKEN --auth-env MYAPP_MCP_TOKEN
```

Then point Ava's manifest at it:

```yaml
mcp:
  url: "http://127.0.0.1:9310/mcp"
  token_env: MYAPP_MCP_TOKEN
```

If your app serves `/.well-known/ava.json`, its `tools` / `call` / `label`
prefill automatically.

**Credentials are named, never passed.** `--token-env` and `--auth-env` take the
*name* of an environment variable, so no secret lands in `argv` where `ps` can
read it. They are two different credentials:

| flag | guards | who presents it |
|---|---|---|
| `--token-env` | the **upstream app** | the adapter, calling your facade |
| `--auth-env` | the **MCP endpoint** | the MCP client, calling the adapter |

Leave `--auth-env` off only when the port is unreachable from anywhere you don't
trust — without it, anyone who can reach it can call every tool.

**The startup warning does not cover you here.** The adapter warns only when you
*name* a variable that turns out to be empty (`--auth-env MYAPP_MCP_TOKEN` with
`MYAPP_MCP_TOKEN` unset). Omit the flag entirely and it starts wide open and
silent — the only signal is the missing `(bearer-protected)` suffix on the
startup line. Always pass `--auth-env`.

## Mount it in-process instead

For a Python app that would rather not run a second process:

```python
import os

from ava_mcp import RegistrySource, serve_mcp

source = RegistrySource(
    tools=[{"name": "list_things", "description": "…",
            "inputSchema": {"type": "object", "properties": {}},
            "access": "read"}],
    dispatch=lambda name, args: my_handlers[name](args),   # -> result or (result, status)
)
serve_mcp(source, port=9310, auth_token=os.environ.get("MYAPP_MCP_TOKEN"), block=False)
```

`dispatch` may return a bare result or `(result, status)` — a status ≥ 400 comes
back to the model as an MCP `isError` result, which is what you want: the model
reads the message and tries something else, instead of the call blowing up.

## `access` tiers survive the conversion

This is the part a hand-rolled MCP server gets wrong. The facade's `access`
field (`read` | `write` | `destructive`) is what drives Ava's just-in-time
consent — `read` runs silently, `write` asks once, `destructive` asks every
time. Plain MCP has no such field, so a naive port silently demotes every
`read` tool to `write` and the operator starts getting prompted for things that
used to be quiet.

`ava_mcp` carries it through on every `tools/list` entry, twice over: top-level
`access` (where Ava's `tools_cache` reads it) and mirrored under `_meta` as
`ava/access` (the spec-sanctioned home for vendor fields, for strict clients).

Tiers remain self-reported and can only make a tool *quieter* — egress policy,
the operator's gate, and the audit ledger all stay on Ava's side.

## What it implements

`initialize` · `notifications/initialized` · `ping` · `tools/list` ·
`tools/call`, over **Streamable HTTP** (POST JSON-RPC; `Mcp-Session-Id` issued
on initialize and echoed thereafter). Tools only — no resources, prompts, or
server→client streaming, because that is all the connector SDK bridges.

`GET /health` is always open so a connector's `service.probe` can see the
adapter is up without holding a credential.

## API

| Symbol | Purpose |
|---|---|
| `FacadeSource(base_url, token, tools_path, call_path, timeout)` | front a running `ava-tools/1` facade |
| `RegistrySource(tools, dispatch)` | serve an in-process tool registry |
| `serve_mcp(source, host, port, path, auth_token, server_name, block, instructions)` | run the MCP server |
| `python -m ava_mcp --facade URL` | the sidecar CLI |

Defaults: `serve_mcp` binds `127.0.0.1:9300` at `/mcp` and **blocks**; pass
`block=False` to run it on a daemon thread. The sidecar CLI additionally takes
`--host`, `--path`, `--tools-path`, `--call-path` and `--name`
(`python -m ava_mcp --help`).
