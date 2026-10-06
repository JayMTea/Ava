#!/usr/bin/env python3
"""Expose a host-local Ava service to the OpenClaw sandbox ONLY (never the LAN).

Services bind 127.0.0.1:<AVA_GW_PORT> (loopback), so they are never reachable
from wifi/tailnet directly. Ava's sandbox reaches the host at
`host.openshell.internal`, which resolves to the sandbox's docker-bridge GATEWAY
ip. This forwards that <gateway>:<port> -> 127.0.0.1:<port> so only containers
on that bridge (and the host itself) can reach the service.

The gateway ip is discovered dynamically (it changes when the sandbox network is
recreated by a `rebuild`), and this runs as its own unit separate from uvicorn so
a sandbox rebuild can never take the core bridge down — only this optional path.
"""
import asyncio
import os
import subprocess
import sys

SANDBOX = os.environ.get("AVA_OC_SANDBOX", "my-assistant")
PORT = int(os.environ.get("AVA_GW_PORT", "8096"))
TARGET = ("127.0.0.1", PORT)


def gateway_ip() -> str:
    """The docker-bridge gateway ip the sandbox uses to reach the host."""
    if os.environ.get("AVA_GW_BIND"):
        return os.environ["AVA_GW_BIND"]
    names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=10).stdout.split()
    cname = next((n for n in names if n.startswith(f"openshell-{SANDBOX}-")), None)
    if not cname:
        raise RuntimeError(f"sandbox container openshell-{SANDBOX}-* not running")
    out = subprocess.run(
        ["docker", "inspect", "-f",
         "{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}", cname],
        capture_output=True, text=True, timeout=10).stdout.split()
    ip = next((x for x in out if x and x != "<no value>"), None)
    if not ip:
        raise RuntimeError(f"no gateway ip for {cname}")
    return ip


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _handle(client_r, client_w):
    try:
        srv_r, srv_w = await asyncio.open_connection(*TARGET)
    except Exception:  # noqa: BLE001
        client_w.close()
        return
    await asyncio.gather(_pipe(client_r, srv_w), _pipe(srv_r, client_w))


async def main():
    bind = gateway_ip()
    server = await asyncio.start_server(_handle, bind, PORT)
    print(f"[ava-gw] forwarding {bind}:{PORT} -> 127.0.0.1:{PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(f"[ava-gw] {e}", file=sys.stderr, flush=True)
        sys.exit(1)
