"""Where NemoClaw/OpenShell put things, and how the sandbox reaches the host.

This is VENDOR KNOWLEDGE — the sandbox's filesystem layout and the docker
network's topology — and it belongs in the runtime package with the adapters
that own the vendor, not scattered through Ava's core modules. It was spread
across seven of them (`connectors.py`, `internal.py`, `policy_inventory.py`,
`setup_wizard.py`, `provision.py`, `gw_forward.py`, `hub/agent.py`), which is
what makes "swap the runtime" a change you cannot make without reading all of
Ava.

Nothing here is a guess: every path and name below is what the running sandbox
actually uses, and `tests/test_nemoclaw_layout.py` holds the call sites to it.

Kept deliberately small. This is not an abstraction over runtimes — a second
runtime with a different layout gets its OWN module, and the seam that chooses
between them is `AgentRuntime`, not a flag in here.
"""
from __future__ import annotations

#: How a process INSIDE the sandbox names the host. It resolves to the docker
#: bridge's GATEWAY ip (e.g. 172.27.0.1), so traffic to it lands in the host's
#: INPUT chain — which is why `deploy/ava-sandbox-firewall.sh` exists at all.
BRIDGE_HOST = "host.openshell.internal"

#: The sandbox's own root, and OpenClaw's directory inside it.
SANDBOX_ROOT = "/sandbox"
OPENCLAW_DIR = f"{SANDBOX_ROOT}/.openclaw"

#: The agent's workspace — the ONLY tree `agents.files.get` will serve. Skills
#: live outside it, which is why they are not observable through the gateway.
WORKSPACE_DIR = f"{OPENCLAW_DIR}/workspace"
SKILLS_DIR = f"{OPENCLAW_DIR}/skills"

#: Files Ava reads or writes by name.
PERSONA_PATH = f"{WORKSPACE_DIR}/IDENTITY.md"
SKILLS_GLOB = f"{SKILLS_DIR}/*/SKILL.md"
CONFIG_PATH = f"{OPENCLAW_DIR}/openclaw.json"


def mcp_server_dir(category: str) -> str:
    """Where a generated MCP tool server for `category` is deployed."""
    return f"{OPENCLAW_DIR}/mcp_server_{category}"


def bridge_url(port: int) -> str:
    """The bridge's address AS THE SANDBOX SEES IT.

    The port is passed in rather than read here on purpose: it is Ava's own
    `server.port`, resolved per call by the caller, and freezing it in a vendor
    module is how a rendered egress policy ends up allowing a port the rewrite
    no longer uses.
    """
    return f"http://{BRIDGE_HOST}:{port}"
