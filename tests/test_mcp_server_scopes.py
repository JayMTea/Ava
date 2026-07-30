"""Every MCP server's capability group must grant exactly what its tools call.

`INTERNAL_SCOPE_GROUPS` is a hand-maintained table, and its own comment says the
sets are "derived from what each agent/mcp_server_<group>/ ACTUALLY calls, not from
what sounds tidy". Nothing checked that. The result:

  * `content` — the group whose server runs `web_fetch`, i.e. the one place
    attacker-controlled text arrives — carried the `connectors` scope, because
    `mcp_server_content` held both the generated per-app tools and the
    device-event tool. So a prompt-injected page could reach every connected app's
    action bridge and every device actuation with the content token.
  * The `connectors` group was minted by `agent/install.sh` and enforced by
    `group_may()`, but no `mcp_server_connectors/` existed, so it granted nothing
    to nobody. `SECURITY.md` §3's claim that the injection-bearing group's blast
    radius is bounded was not true of connectors.

This module derives the requirement instead of trusting the table: it reads the
`/internal/...` paths each server's tools actually reference, maps them through
`ROUTE_SCOPES`, and compares. It fails both ways — a missing scope (tools break)
and a surplus one (quiet over-grant, which is what happened).

Static: no bridge, no sandbox, no AVA_HOME. It reads the tool sources on disk.
"""
import pathlib
import re
import unittest

from ava_bridge.internal import ROUTE_SCOPES, _TOKEN_GROUPS, required_scope
from ava_bridge.security import INTERNAL_SCOPE_GROUPS

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"

# Any `/internal/...` literal in a tool source. Brace/angle placeholders are how
# the generated tools template their cid, so allow them and let required_scope
# resolve on the prefix.
_ROUTE = re.compile(r"/internal/[A-Za-z0-9_./${}<>-]*")


def _servers() -> dict[str, pathlib.Path]:
    return {d.name[len("mcp_server_"):]: d
            for d in sorted(AGENT.glob("mcp_server_*"))
            if (d / "_server.mjs").is_file()}


def _scopes_called_by(server_dir: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for f in server_dir.rglob("*.mjs"):
        if f.name.startswith(("_", ".")):
            continue
        for raw in _ROUTE.findall(f.read_text(encoding="utf-8", errors="replace")):
            # Strip a templated tail so "/internal/connector/{cid}/x" resolves on
            # its declared prefix.
            sc = required_scope(raw) or required_scope(raw.split("{")[0].split("<")[0])
            if sc:
                found.add(sc)
    return found


class ServerScopeDerivationTests(unittest.TestCase):
    def test_a_connectors_server_exists_for_the_connectors_group(self):
        """The group was minted and enforced with nothing behind it."""
        self.assertIn("connectors", _servers(),
                      "INTERNAL_SCOPE_GROUPS and install.sh both define a "
                      "`connectors` capability group; it needs a server, or the "
                      "group is a promise with no subject.")

    def test_install_sh_will_discover_every_server(self):
        """install.sh keys on `mcp_server_*/` containing `_server.mjs`."""
        for cat, d in _servers().items():
            self.assertTrue((d / "_server.mjs").is_file(), cat)
            self.assertTrue((d / "_lib.mjs").is_file(),
                            f"{cat}: install.sh tars each server dir separately, so "
                            "each needs its own _lib.mjs copy")

    def test_every_server_is_a_known_token_group(self):
        for cat in _servers():
            self.assertIn(cat, _TOKEN_GROUPS, f"{cat} has no token group")
            self.assertIn(cat, INTERNAL_SCOPE_GROUPS, f"{cat} has no scope set")

    def test_no_server_is_missing_a_scope_its_tools_call(self):
        for cat, d in _servers().items():
            need = _scopes_called_by(d)
            have = INTERNAL_SCOPE_GROUPS.get(cat, frozenset())
            missing = need - set(have)
            self.assertEqual(missing, set(),
                             f"mcp_server_{cat} calls routes needing {sorted(missing)} "
                             f"but its group grants {sorted(have)} — those tools "
                             "would 403.")

    def test_no_server_holds_a_scope_none_of_its_tools_call(self):
        """The direction that actually bit. A surplus scope breaks nothing, so it
        survives — which is exactly why `content` kept `connectors` after the tools
        that needed it were the only justification for it.

        `model` is exempt: it is a read-only capability several servers legitimately
        hold for status, and it is not reachable from a tool source in every case.
        """
        for cat, d in _servers().items():
            need = _scopes_called_by(d) | {"model"}
            surplus = set(INTERNAL_SCOPE_GROUPS.get(cat, frozenset())) - need
            self.assertEqual(surplus, set(),
                             f"mcp_server_{cat}'s group grants {sorted(surplus)} but "
                             "no tool in it calls a route needing that. Remove the "
                             "scope, or move the tool that needs it into this server.")

    def test_the_injection_bearing_group_cannot_reach_connectors_or_code(self):
        """`content` runs web_fetch. Name the two scopes it must never hold."""
        content = INTERNAL_SCOPE_GROUPS["content"]
        self.assertNotIn("code_change", content)
        self.assertNotIn("connectors", content,
                         "content runs web_fetch, so this hands every connected "
                         "app's action bridge and every device actuation to a "
                         "prompt-injected page.")

    def test_the_moved_tools_landed_in_the_connectors_server(self):
        conn = _servers()["connectors"]
        self.assertTrue((conn / "devices" / "device_events.mjs").is_file(),
                        "device_events calls /internal/devices/events, which is "
                        "connectors-scoped, so it belongs to this server")
        self.assertFalse((AGENT / "mcp_server_content" / "devices").exists())

    def test_every_connectors_scoped_route_is_reachable_from_that_server(self):
        conn_routes = [p for p, s in ROUTE_SCOPES.items() if s == "connectors"]
        self.assertTrue(conn_routes)
        for p in conn_routes:
            self.assertTrue(required_scope(p) == "connectors", p)


if __name__ == "__main__":
    unittest.main()
