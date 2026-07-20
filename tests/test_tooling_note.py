"""Undeployed-tools awareness: Ava must say "deploy first", never shrug/invent."""
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors, features, turns


def _write(base, cid, body):
    d = os.path.join(base, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


class UndeployedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = tempfile.mkdtemp()          # fake CODE root (agent content)
        self._p = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.tmp),
            mock.patch.object(connectors, "USER_DIR", self.tmp),
            mock.patch.object(connectors.config, "ROOT", self.root),
        ]
        for p in self._p:
            p.start()
        connectors._undeployed_cache.update(ts=0.0, list=None)
        _write(self.tmp, "myapp", """\
            id: myapp
            label: My App
            actions:
              discover: { base: "http://127.0.0.1:9", list: /tools, call: /call }
        """)
        connectors.load(force=True)

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_missing_tool_files_reported(self):
        out = connectors.undeployed()
        self.assertEqual([m["id"] for m in out], ["myapp"])
        self.assertEqual(out[0]["tools"], 2)     # find_tool + call meta pair

    def test_deployed_connector_not_reported(self):
        d = os.path.join(self.root, "agent", "mcp_server_content", "connectors", "myapp")
        os.makedirs(d)
        for t in connectors.tool_files("myapp"):
            open(os.path.join(d, t["name"]), "w").write(t["source"])
        connectors._undeployed_cache.update(ts=0.0, list=None)
        self.assertEqual(connectors.undeployed(), [])


class NoteTests(unittest.TestCase):
    def test_agent_note_names_apps_and_deploy(self):
        with mock.patch.object(connectors, "undeployed",
                               return_value=[{"id": "a", "label": "My App", "tools": 2}]):
            note = turns._tooling_note(direct=False)
        self.assertIn("My App (2 tools)", note)
        self.assertIn("Deploy", note)
        self.assertIn("Never invent", note)

    def test_agent_note_carries_credentials_stance_when_all_deployed(self):
        # No undeployed apps, but the standing credentials stance still rides
        # every turn so "what's my password" is a pointer, not a dead end.
        with mock.patch.object(connectors, "undeployed", return_value=[]):
            note = turns._tooling_note(direct=False)
        self.assertNotIn("Deploy on that app", note)
        self.assertIn("credentials", note.lower())
        self.assertIn("password", note.lower())

    def test_direct_note_always_warns(self):
        with mock.patch.object(connectors, "undeployed", return_value=[]):
            note = turns._tooling_note(direct=True)
        self.assertIn("NO app tools", note)

    def test_credentials_note_points_at_connected_apps(self):
        with mock.patch.object(connectors, "apps",
                               return_value=[{"id": "persona", "label": "Persona"}]):
            note = turns._credentials_note()
        self.assertIn("Persona", note)
        self.assertIn("don't keep", note.lower().replace("do not", "don't"))

    def test_credentials_note_forbids_access_and_revealing(self):
        # HARD INVARIANT (owner instruction): Ava must never have access to
        # passwords and must never reveal one. Guard the exact stance so a
        # future edit can't weaken it to "I can look that up".
        note = turns._credentials_note().lower()
        self.assertIn("do not store, retrieve, or have access", note)
        self.assertIn("never reveal", note)
        for word in ("passwords", "api keys", "credentials"):
            self.assertIn(word, note)

    def test_agent_note_states_feature_switches(self):
        # Ava once denied having web access because the sandbox preamble says
        # outbound network is deny-by-default (chat 2026-07-19). The agent-path
        # note must affirmatively carry host-mediation + per-feature switch
        # state so she answers "that's switched off", never "I can't".
        snap = [{"key": "web_search", "label": "Web search", "sub": "s",
                 "enabled": False},
                {"key": "image", "label": "Image / video generation", "sub": "s",
                 "enabled": True}]
        with mock.patch.object(connectors, "undeployed", return_value=[]), \
             mock.patch.object(features, "snapshot", return_value=snap):
            note = turns._tooling_note(direct=False)
        self.assertIn("host-mediated", note)
        self.assertIn("Web search: OFF", note)
        self.assertIn("Image / video generation: on", note)
        self.assertIn("Optional features", note)
        self.assertIn("switched off", note)

    def test_agent_note_counters_sandbox_network_policy(self):
        # The affirmative "sandbox no-internet ≠ no web access" line must ride
        # the agent path in BOTH deployed/undeployed shapes.
        for missing in ([], [{"id": "a", "label": "My App", "tools": 2}]):
            with mock.patch.object(connectors, "undeployed",
                                   return_value=missing):
                note = turns._tooling_note(direct=False)
            self.assertIn("does NOT mean you lack web access", note,
                          f"network-policy counter missing "
                          f"(undeployed={bool(missing)})")

    def test_direct_note_has_no_capability_states(self):
        # The tool-less direct runtime has NO tools at all — feature switch
        # states would contradict the "no app tools this turn" warning.
        with mock.patch.object(connectors, "undeployed", return_value=[]):
            note = turns._tooling_note(direct=True)
        self.assertNotIn("host-mediated", note)
        self.assertNotIn("Optional features right now", note)

    def test_capabilities_note_survives_snapshot_failure(self):
        # Awareness must never break a turn: a snapshot() blow-up degrades to
        # "no capabilities note", keeping the credentials stance intact.
        with mock.patch.object(connectors, "undeployed", return_value=[]), \
             mock.patch.object(features, "snapshot",
                               side_effect=RuntimeError("boom")):
            note = turns._tooling_note(direct=False)
        self.assertIn("never reveal", note.lower())
        self.assertNotIn("Optional features right now", note)

    def test_every_turn_note_carries_the_credentials_stance(self):
        # Both runtime paths (active agent + tool-less direct) and both
        # deployed/undeployed states must include the stance — it can never be
        # dropped for any turn shape.
        for direct in (True, False):
            for missing in ([], [{"id": "a", "label": "My App", "tools": 2}]):
                with mock.patch.object(connectors, "undeployed", return_value=missing):
                    note = turns._tooling_note(direct=direct).lower()
                self.assertIn("never reveal", note,
                              f"credentials stance missing (direct={direct}, "
                              f"undeployed={bool(missing)})")


if __name__ == "__main__":
    unittest.main()
