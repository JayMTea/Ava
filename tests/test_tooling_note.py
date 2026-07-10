"""Undeployed-tools awareness: Ava must say "deploy first", never shrug/invent."""
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors, turns


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

    def test_agent_note_empty_when_all_deployed(self):
        with mock.patch.object(connectors, "undeployed", return_value=[]):
            self.assertEqual(turns._tooling_note(direct=False), "")

    def test_direct_note_always_warns(self):
        with mock.patch.object(connectors, "undeployed", return_value=[]):
            note = turns._tooling_note(direct=True)
        self.assertIn("NO app tools", note)


if __name__ == "__main__":
    unittest.main()
