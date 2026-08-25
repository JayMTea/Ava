"""The names a container runtime gives the MACHINE it is running on.

One table, two consumers, and they must agree:

* the setup wizard probes these names to find a native inference engine that
  lives outside the bridge's own container (`setup_wizard.host_gateway`);
* the bridge's DNS-rebinding guard (`auth._LOCAL_HOSTNAMES`) must ANSWER to
  them — the agent sandbox reaches the bridge by exactly such a name, and a
  name missing here is refused 421 "untrusted host" on every tool callback,
  while the owner's browser keeps working. That asymmetry is how the bridge
  once shipped with every connector wired and none reachable from chat.

Docker Desktop publishes the first (a Linux compose file opts in with
`host-gateway`), Podman the second, the OpenShell/NemoClaw sandbox the third.
This is not vendor coupling: each is one member of a cross-runtime table of
container-host gateway names (see the allowance in tests/test_nemoclaw_layout.py),
and the question both consumers ask is "what does any container call its host".
"""
from __future__ import annotations

HOST_GATEWAYS: tuple[str, ...] = ("host.docker.internal",
                                  "host.containers.internal",
                                  "host.openshell.internal")
