"""Ava inference router — standalone entrypoint.

The router logic lives in agent-platform/integrations/ava/ava_agent/router_app.py (an app factory shared by
this standalone entrypoint and the bridge's embedded router). Run standalone:

    uvicorn app.router:app --host 127.0.0.1 --port 8010

Bind to loopback unless you know you need LAN exposure — with a non-loopback
host every /v1/* call requires the shared router token as a Bearer /
X-Ava-Router-Token header (see agent-platform/integrations/ava/ava_agent/router_app.py). Note: `ava up`
starts this same router in-process by default (agent-platform/integrations/ava/ava_agent/router_host.py), so
a standalone unit is only needed for always-on installs; the embedded starter
detects a running router and yields.
"""
from ava_agent.router_app import create_app

app = create_app()
