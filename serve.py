#!/usr/bin/env python3
"""Ava bridge entrypoint.

Binds host/port resolved from config (env AVA_HOST/AVA_PORT -> $AVA_HOME/ava.yaml
server.* -> defaults), so editing ava.yaml actually moves the server. Used by the
Docker image and run_bridge.sh.
"""
import uvicorn

from ava_bridge import config

if __name__ == "__main__":
    uvicorn.run("phone_bridge:app", host=config.SERVER_HOST, port=config.SERVER_PORT)
