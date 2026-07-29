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


class MetaToolsTests(unittest.TestCase):
    """Dynamic tool loading: big static connectors swap N per-action tools for
    find/call meta tools on the __tools/__call bridge routes."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._p = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.tmp),
            mock.patch.object(connectors, "USER_DIR", self.tmp),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _app(self, n):
        acts = "\n".join(
            f"  - id: act{i}\n    method: POST\n    path: /api/thing{i}\n"
            f"    description: does thing {i}" for i in range(n))
        _write(self.tmp, "app",
               f"id: app\nbase_url: http://127.0.0.1:9\nactions:\n{acts}\n")
        connectors.load(force=True)

    def test_small_connector_keeps_per_action_tools(self):
        self._app(connectors.META_TOOLS_MIN - 1)
        names = [t["name"] for t in connectors.tool_files("app")]
        self.assertEqual(len(names), connectors.META_TOOLS_MIN - 1)
        self.assertIn("app_act0.mjs", names)

    def test_large_connector_gets_two_meta_tools(self):
        self._app(connectors.META_TOOLS_MIN)
        names = [t["name"] for t in connectors.tool_files("app")]
        self.assertEqual(names, ["app_find_tool.mjs", "app_call.mjs"])

    def test_egress_meta_mode_uses_bridge_routes_only(self):
        self._app(connectors.META_TOOLS_MIN)
        pol = connectors.render_egress_policy("app")
        rules = [r["allow"]["path"]
                 for np in pol["network_policies"].values()
                 for ep in np["endpoints"] for r in ep.get("rules", [])]
        self.assertIn("/internal/connector/app/__tools", rules)
        self.assertIn("/internal/connector/app/__call", rules)
        self.assertFalse([p for p in rules if "/app/act" in p])

    def test_egress_small_mode_keeps_per_action_routes(self):
        self._app(3)
        pol = connectors.render_egress_policy("app")
        rules = [r["allow"]["path"]
                 for np in pol["network_policies"].values()
                 for ep in np["endpoints"] for r in ep.get("rules", [])]
        self.assertIn("/internal/connector/app/act0", rules)
        self.assertNotIn("/internal/connector/app/__call", rules)

    def test_policy_name_not_doubled_for_ava_prefixed_id(self):
        # Regression: a connector id that already starts with "ava-"
        # must keep that as its egress preset name, not become "ava-ava-<id>".
        _write(self.tmp, "ava-foo",
               "id: ava-foo\nbase_url: http://127.0.0.1:9\n"
               "actions:\n  - id: act0\n    method: GET\n    path: /x\n")
        connectors.load(force=True)
        pol = connectors.render_egress_policy("ava-foo")
        self.assertEqual(pol["preset"]["name"], "ava-foo")
        self.assertIn("ava-foo", pol["network_policies"])
        self.assertNotIn("ava-ava-foo", pol["network_policies"])

    def test_policy_name_prefixed_for_plain_id(self):
        _write(self.tmp, "foo",
               "id: foo\nbase_url: http://127.0.0.1:9\n"
               "actions:\n  - id: act0\n    method: GET\n    path: /x\n")
        connectors.load(force=True)
        self.assertEqual(connectors.render_egress_policy("foo")["preset"]["name"], "ava-foo")

    def test_discover_tools_serves_static_actions_with_search(self):
        self._app(20)
        all_tools = connectors.discover_tools("app")
        self.assertEqual(len(all_tools["tools"]), 20)
        hit = connectors.discover_tools("app", query="thing7", limit=5)
        self.assertEqual(hit["tools"][0]["name"], "act7")   # best match first
        self.assertLessEqual(len(hit["tools"]), 5)

    def test_call_discovered_falls_back_to_static_action(self):
        self._app(20)
        with mock.patch.object(connectors, "call_action",
                               return_value=({"ok": True}, 200)) as ca:
            data, status = connectors.call_discovered("app", "act3", {"x": 1})
            ca.assert_called_once_with("app", "act3", {"x": 1})
        self.assertEqual(status, 200)
        data, status = connectors.call_discovered("app", "nope", {})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
