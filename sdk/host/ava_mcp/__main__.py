"""Run the adapter as a sidecar: front an ava-tools/1 app with real MCP.

    python -m ava_mcp --facade http://127.0.0.1:8097 --port 9310 \
                      --token-env MYAPP_TOKEN --auth-env MYAPP_MCP_TOKEN

Then point Ava's manifest at it — no code change in the app itself:

    mcp:
      url: "http://127.0.0.1:9310/mcp"
      token_env: MYAPP_MCP_TOKEN

Credentials are named, never passed: `--token-env` / `--auth-env` take the NAME
of an environment variable, so no secret ever lands in argv where `ps` can read
it. If the app serves /.well-known/ava.json, its `tools` / `call` / `label`
prefill automatically.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from .ava_mcp import FacadeSource, serve_mcp


def _well_known(base: str, token: str | None) -> dict:
    """The app's self-description, or {} — best-effort, never fatal."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(base.rstrip("/") + "/.well-known/ava.json",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read() or b"{}")
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — self-description is optional
        return {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m ava_mcp",
        description="Serve an ava-tools/1 facade as a real MCP server.")
    p.add_argument("--facade", required=True,
                   help="base URL of the app's ava-tools/1 facade, e.g. http://127.0.0.1:8097")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1 — keep it local)")
    p.add_argument("--port", type=int, default=9300, help="bind port (default 9300)")
    p.add_argument("--path", default="/mcp", help="MCP endpoint path (default /mcp)")
    p.add_argument("--tools-path", default=None, help="override GET tools path")
    p.add_argument("--call-path", default=None, help="override POST call path")
    p.add_argument("--token-env", default=None,
                   help="env var holding the credential for the UPSTREAM app")
    p.add_argument("--auth-env", default=None,
                   help="env var holding the bearer token that guards THIS endpoint")
    p.add_argument("--name", default=None, help="server name reported to MCP clients")
    a = p.parse_args(argv)

    upstream = os.environ.get(a.token_env or "", "").strip() or None
    if a.token_env and not upstream:
        print(f"[ava_mcp] warning: {a.token_env} is unset — calling the facade "
              "unauthenticated", file=sys.stderr)
    auth = os.environ.get(a.auth_env or "", "").strip() or None
    if a.auth_env and not auth:
        print(f"[ava_mcp] warning: {a.auth_env} is unset — this MCP endpoint is "
              "UNAUTHENTICATED. Anyone who can reach the port can call every tool.",
              file=sys.stderr)

    wk = _well_known(a.facade, upstream)
    tools_path = a.tools_path or wk.get("tools") or "/tools"
    call_path = a.call_path or wk.get("call") or "/call"
    name = a.name or wk.get("label") or "ava-mcp-adapter"

    source = FacadeSource(a.facade, token=upstream,
                          tools_path=tools_path, call_path=call_path)
    try:
        n = len(source.list_tools())
    except Exception as e:  # noqa: BLE001 — start anyway; the app may boot later
        n = -1
        print(f"[ava_mcp] warning: could not reach the facade yet ({e})", file=sys.stderr)

    print(f"[ava_mcp] {name}: {a.facade}{tools_path} "
          f"({n if n >= 0 else '?'} tools) -> MCP at "
          f"http://{a.host}:{a.port}{a.path}"
          f"{' (bearer-protected)' if auth else ''}", flush=True)
    serve_mcp(source, host=a.host, port=a.port, path=a.path,
              auth_token=auth, server_name=name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
