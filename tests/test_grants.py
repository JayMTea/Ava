"""JIT consent: access tiers, the grants store, and the tier-aware gate."""
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from ava_bridge import approvals, connectors, grants


def _write(base, cid, body):
    d = os.path.join(base, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


APP = """\
id: app
actions:
  - { id: list_things,  method: GET,    path: /api/things }
  - { id: make_thing,   method: POST,   path: /api/things }
  - { id: drop_thing,   method: POST,   path: /api/things/delete }
  - { id: gated_thing,  method: POST,   path: /api/gated, confirm: true }
  - { id: costly_read,  method: GET,    path: /api/export, access: write }
  - { id: open_valve,   method: POST,   path: /api/valve, access: physical }
"""

# A device connector with dynamically-discovered tools (MCP-style) classified
# by the manifest's dynamic_access patterns — the Home Assistant shape.
DEVICE = """\
id: hub
role: device
mcp: { url: "http://127.0.0.1:1/sse" }
dynamic_access:
  GetLiveContext: read
  "Vacuum*": write
  "*": physical
"""

# A device connector that declares NO dynamic_access — unknown tools must
# still land on the safe default (physical), not silently become grantable.
BAREDEV = """\
id: baredev
role: device
mcp: { url: "http://127.0.0.1:1/sse" }
"""


class TierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._p = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.tmp),
            mock.patch.object(connectors, "USER_DIR", self.tmp),
            mock.patch.object(grants, "PATH",
                              os.path.join(self.tmp, "connector_grants.yaml")),
            mock.patch("ava_bridge.audit.record"),
        ]
        for p in self._p:
            p.start()
        grants._cache.update(data=None, mtime=0.0)
        _write(self.tmp, "app", APP)
        _write(self.tmp, "hub", DEVICE)
        _write(self.tmp, "baredev", BAREDEV)
        connectors.load(force=True)

    def tearDown(self):
        for p in self._p:
            p.stop()

    # --- tier inference -----------------------------------------------------
    def test_get_is_read_post_is_write_delete_path_is_destructive(self):
        self.assertEqual(connectors.action_access("app", "list_things"), "read")
        self.assertEqual(connectors.action_access("app", "make_thing"), "write")
        self.assertEqual(connectors.action_access("app", "drop_thing"), "destructive")

    def test_explicit_access_overrides_method(self):
        self.assertEqual(connectors.action_access("app", "costly_read"), "write")

    def test_dynamic_tools_default_to_write(self):
        self.assertEqual(connectors.action_access("app", "mystery_tool"), "write")

    # --- the gate -----------------------------------------------------------
    def test_read_runs_silently(self):
        self.assertFalse(connectors.needs_confirm("app", "list_things"))

    def test_write_asks_until_granted(self):
        self.assertTrue(connectors.needs_confirm("app", "make_thing"))
        grants.grant("app", "make_thing")
        self.assertFalse(connectors.needs_confirm("app", "make_thing"))

    def test_destructive_asks_even_when_granted(self):
        grants.grant("app", "drop_thing")   # a rogue grant must not bypass
        self.assertTrue(connectors.needs_confirm("app", "drop_thing"))

    def test_author_confirm_sticks_despite_grant(self):
        grants.grant("app", "gated_thing")
        self.assertTrue(connectors.needs_confirm("app", "gated_thing"))

    def test_grantable_write_only(self):
        self.assertTrue(connectors.grantable("app", "make_thing"))
        self.assertFalse(connectors.grantable("app", "drop_thing"))     # destructive
        self.assertFalse(connectors.grantable("app", "gated_thing"))    # author gate
        self.assertFalse(connectors.grantable("app", "list_things"))    # read
        self.assertFalse(connectors.grantable("app", "open_valve"))     # physical

    # --- the physical tier (real-world actuation is never silent) ------------
    def test_physical_is_declared_never_inferred(self):
        self.assertEqual(connectors.action_access("app", "open_valve"), "physical")
        # a plain POST stays write — physical only comes from a declaration
        self.assertEqual(connectors.action_access("app", "make_thing"), "write")

    def test_physical_asks_even_when_granted(self):
        grants.grant("app", "open_valve")   # a rogue grant must not bypass
        self.assertTrue(connectors.needs_confirm("app", "open_valve"))

    def test_dynamic_access_patterns_classify_mcp_tools(self):
        self.assertEqual(connectors.action_access("hub", "GetLiveContext"), "read")
        self.assertEqual(connectors.action_access("hub", "VacuumStart"), "write")
        self.assertEqual(connectors.action_access("hub", "HassTurnOn"), "physical")
        self.assertFalse(connectors.needs_confirm("hub", "GetLiveContext"))
        self.assertTrue(connectors.needs_confirm("hub", "HassTurnOn"))
        self.assertFalse(connectors.grantable("hub", "HassTurnOn"))

    def test_device_unknown_dynamic_tools_default_to_physical(self):
        self.assertEqual(connectors.action_access("baredev", "mystery_verb"), "physical")
        grants.grant("baredev", "mystery_verb")   # rogue grant must not bypass
        self.assertTrue(connectors.needs_confirm("baredev", "mystery_verb"))

    # --- capability grouping (the permissions sheet) --------------------------
    def test_capability_from_path_skips_api_prefix_and_params(self):
        self.assertEqual(
            connectors.action_capability({"path": "/api/persona/{key}/lock"}),
            "persona")
        self.assertEqual(connectors.action_capability({"path": "/v1/things"}), "things")
        self.assertEqual(connectors.action_capability({"path": ""}), "general")

    def test_explicit_capability_wins(self):
        self.assertEqual(
            connectors.action_capability({"capability": "Media", "path": "/api/x"}),
            "media")

    # --- store round-trip ---------------------------------------------------
    def test_grant_revoke_roundtrip_survives_reload(self):
        grants.grant("app", "make_thing")
        grants._cache.update(data=None, mtime=0.0)   # force re-read from disk
        self.assertTrue(grants.has("app", "make_thing"))
        self.assertTrue(grants.revoke("app", "make_thing"))
        self.assertFalse(grants.has("app", "make_thing"))
        self.assertFalse(grants.revoke("app", "make_thing"))  # already gone


class DecideRememberTests(unittest.TestCase):
    def _pend(self, grantable):
        approvals._pending["t1"] = {
            "id": "t1", "connector": "app", "action": "make_thing",
            "args": {}, "status": "pending", "ts": 0.0,
            "grantable": grantable, "access": "write"}
        self.addCleanup(approvals._pending.pop, "t1", None)

    def test_always_writes_a_grant(self):
        self._pend(grantable=True)
        with mock.patch.object(grants, "grant") as g:
            self.assertTrue(approvals.decide("t1", True, remember=True))
            g.assert_called_once_with("app", "make_thing")

    def test_remember_ignored_when_not_grantable(self):
        self._pend(grantable=False)
        with mock.patch.object(grants, "grant") as g:
            self.assertTrue(approvals.decide("t1", True, remember=True))
            g.assert_not_called()

    def test_deny_never_grants(self):
        self._pend(grantable=True)
        with mock.patch.object(grants, "grant") as g:
            self.assertTrue(approvals.decide("t1", False, remember=True))
            g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
