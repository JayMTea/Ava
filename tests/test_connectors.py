"""Connector registry parsing + capability accessors (tmp-dir fixtures)."""
import os
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors


def _write(base, cid, body):
    d = os.path.join(base, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.user = os.path.join(self.tmp, "user")
        self.builtin = os.path.join(self.tmp, "builtin")
        os.makedirs(self.user)
        os.makedirs(self.builtin)
        self._p = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.builtin),
            mock.patch.object(connectors, "USER_DIR", self.user),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _load(self):
        return connectors.load(force=True)

    def test_malformed_yaml_is_skipped(self):
        _write(self.builtin, "ok", "id: ok\nlabel: OK\n")
        d = os.path.join(self.builtin, "bad")
        os.makedirs(d)
        with open(os.path.join(d, "connector.yaml"), "w") as f:
            f.write("{{{ not yaml")
        ids = [m["id"] for m in self._load()]
        self.assertIn("ok", ids)
        self.assertNotIn("bad", ids)

    def test_non_dict_manifest_is_skipped_not_crash(self):
        # A YAML list parses fine but isn't a manifest — must not AttributeError.
        _write(self.builtin, "listy", "- a\n- b\n")
        _write(self.builtin, "ok", "id: ok\n")
        ids = [m["id"] for m in self._load()]
        self.assertEqual(ids, ["ok"])

    def test_id_defaults_to_folder_name(self):
        _write(self.builtin, "myapp", "label: My App\n")
        self.assertEqual(self._load()[0]["id"], "myapp")

    def test_user_overrides_builtin_by_id(self):
        _write(self.builtin, "dup", "id: dup\nlabel: Builtin\n")
        _write(self.user, "dup", "id: dup\nlabel: User\n")
        m = {x["id"]: x for x in self._load()}["dup"]
        self.assertEqual(m["label"], "User")

    def test_disabled_filtered_and_underscore_skipped(self):
        _write(self.builtin, "off", "id: off\nenabled: false\n")
        _write(self.builtin, "_tmpl", "id: tmpl\n")
        _write(self.builtin, "keep", "id: keep\n")
        ids = [m["id"] for m in self._load()]
        self.assertEqual(ids, ["keep"])

    def test_core_kind_sorts_first(self):
        _write(self.builtin, "z-app", "id: z-app\nkind: app\n")
        _write(self.builtin, "a-core", "id: a-core\nkind: core\n")
        ids = [m["id"] for m in self._load()]
        self.assertEqual(ids[0], "a-core")


class ExpandTests(unittest.TestCase):
    def test_default_syntax(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_XYZ_UNSET", None)
            self.assertEqual(
                connectors._expand("${AVA_XYZ_UNSET:-http://d:1}/p"), "http://d:1/p")
        with mock.patch.dict(os.environ, {"AVA_XYZ_SET": "http://real:2"}):
            self.assertEqual(
                connectors._expand("${AVA_XYZ_SET:-http://d:1}/p"), "http://real:2/p")

    def test_plain_env_and_builtin_vars(self):
        with mock.patch.dict(os.environ, {"MYVAR": "abc"}):
            self.assertEqual(connectors._expand("x-${MYVAR}"), "x-abc")
        self.assertTrue(connectors._expand("${AVA_HOME}").strip())


class CapabilityAccessorTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.builtin = os.path.join(self.tmp, "builtin")
        os.makedirs(self.builtin)
        _write(self.builtin, "app", """
            id: app
            ui: { embed: iframe, url: "http://127.0.0.1:9000" }
            chat_pickup:
              after_actions: [render]
              after_tools: [legacy_tool]
              path: /api/log
              params: { kind: render }
              fields: { url: url, seed: seed }
            jobs:
              path: /api/jobs
              engine: MyEngine
              labels: { render: "Content render" }
            model_hints:
              - { match: [mymodel], role: "Rendering" }
        """)
        self._p = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.builtin),
            mock.patch.object(connectors, "USER_DIR", os.path.join(self.tmp, "none")),
        ]
        for p in self._p:
            p.start()
        connectors.load(force=True)

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_chat_pickups(self):
        cp = connectors.chat_pickups()[0]
        self.assertEqual(cp["id"], "app")
        self.assertIn("app_render", cp["tools"])   # <cid>_<action>
        self.assertIn("legacy_tool", cp["tools"])
        self.assertEqual(cp["url"], "http://127.0.0.1:9000/api/log")
        self.assertEqual(cp["url_prefix"], "/apps/app")   # iframe -> proxy prefix

    def test_job_sources(self):
        js = connectors.job_sources()[0]
        self.assertEqual(js["url"], "http://127.0.0.1:9000/api/jobs")
        self.assertEqual(js["engine"], "MyEngine")
        self.assertEqual(js["labels"]["render"], "Content render")

    def test_model_hints(self):
        h = connectors.model_hints()[0]
        self.assertEqual(h["match"], ["mymodel"])
        self.assertEqual(h["role"], "Rendering")


if __name__ == "__main__":
    unittest.main()
