#!/usr/bin/env python3
"""Ava bridge entrypoint.

Binds host/port resolved from config (env AVA_HOST/AVA_PORT -> $AVA_HOME/ava.yaml
server.* -> defaults), so editing ava.yaml actually moves the server. Used by the
Docker image and run_bridge.sh.
"""
import logging
import os
import sys

import uvicorn

from ava_bridge import config

_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def _configure_logging() -> None:
    """Give the package's loggers somewhere to go.

    `ava_bridge` modules use `logging.getLogger(__name__)`, but nothing ever
    configured the root logger — so records below WARNING went nowhere (only
    Python's last-resort handler caught WARNING+), and there was no way to turn
    detail up when something misbehaved. That is why the codebase reaches for
    print() instead: logging did not visibly work, so it stopped being used.

    Set AVA_LOG_LEVEL=DEBUG to turn it up. uvicorn configures its own `uvicorn.*`
    loggers with disable_existing_loggers=False, so this root handler survives
    uvicorn.run() and the two coexist.
    """
    name = os.environ.get("AVA_LOG_LEVEL", "INFO").strip().upper()
    if name not in _LEVELS:
        name = "INFO"
    logging.basicConfig(
        level=getattr(logging, name),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


if __name__ == "__main__":
    _configure_logging()
    # proxy_headers + forwarded_allow_ips: behind `tailscale serve` or a same-host
    # nginx/Caddy, this is what makes request.url.scheme read `https` instead of
    # `http`, which in turn is what lets the session cookie be marked Secure only
    # when it genuinely is. Restricted to config.TRUSTED_PROXIES — accepting these
    # headers from an arbitrary peer would let anyone claim any scheme or address.
    uvicorn.run("phone_bridge:app", host=config.SERVER_HOST, port=config.SERVER_PORT,
                proxy_headers=True, forwarded_allow_ips=list(config.TRUSTED_PROXIES))
