#!/usr/bin/env python3
"""Ava bridge entrypoint.

Binds host/port resolved from config (env AVA_HOST/AVA_PORT -> $AVA_HOME/ava.yaml
server.* -> defaults), so editing ava.yaml actually moves the server. Used by the
Docker image and run_bridge.sh.
"""
import uvicorn

from ava_bridge import config

if __name__ == "__main__":
    # proxy_headers + forwarded_allow_ips: behind `tailscale serve` or a same-host
    # nginx/Caddy, this is what makes request.url.scheme read `https` instead of
    # `http`, which in turn is what lets the session cookie be marked Secure only
    # when it genuinely is. Restricted to config.TRUSTED_PROXIES — accepting these
    # headers from an arbitrary peer would let anyone claim any scheme or address.
    uvicorn.run("phone_bridge:app", host=config.SERVER_HOST, port=config.SERVER_PORT,
                proxy_headers=True, forwarded_allow_ips=list(config.TRUSTED_PROXIES))
