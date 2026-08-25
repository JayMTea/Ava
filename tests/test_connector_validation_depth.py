"""Registry survives any single bad manifest; nested fields are quarantined.

Regression suite for the 2026-08 connector audit: one owner mistake in one
connector.yaml must never take down the registry, the left rail, or the Setup
page that would repair it — and the folder name is the connector's identity.
"""
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors


def _write(base, cid, body):
    d = os.path.join(base, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


class _RegistryCase(unittest.TestCase):
    def setUp(self):
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

    def _errors_after_load(self):
        self._load()
        return connectors.load_errors()


class NonStringIdTests(_RegistryCase):
    def test_numeric_id_no_longer_poisons_the_registry(self):
        # `id: 2048` (an app actually called 2048) used to KeyError the whole
        # registry — every connector vanished, including the Setup page.
        _write(self.builtin, "game2048", "id: 2048\nlabel: '2048'\n")
        _write(self.builtin, "notes", "id: notes\nlabel: Notes\n")
        ids = [m["id"] for m in self._load()]
        self.assertIn("game2048", ids)
        self.assertIn("notes", ids)
        errs = " ".join(e["error"] for e in connectors.load_errors())
        self.assertIn("does not match its folder", errs)

    def test_boolean_id_falls_back_to_folder_with_a_warning(self):
        _write(self.builtin, "switchy", "id: yes\nlabel: Switchy\n")
        ids = [m["id"] for m in self._load()]
        self.assertEqual(ids, ["switchy"])
        errs = connectors.load_errors()
        self.assertTrue(any("boolean" in e["error"] for e in errs))

    def test_non_string_top_level_key_is_ignored_not_fatal(self):
        # `on:` parses to the boolean True as a KEY; `1:` to an int. The
        # unknown-key scan used to AttributeError on `.startswith`.
        _write(self.builtin, "oddkeys", "label: Odd\n1: x\n'on': y\n")
        _write(self.builtin, "notes", "id: notes\n")
        ids = [m["id"] for m in self._load()]
        self.assertEqual(sorted(ids), ["notes", "oddkeys"])

    def test_folder_name_wins_over_declared_id(self):
        # The registry, the Hub routes and the generated files must share ONE
        # identity — the folder. A disagreeing `id:` is reported and ignored.
        _write(self.user, "crm", "id: mycrm\nlabel: CRM\n")
        m = {x["id"]: x for x in self._load()}
        self.assertIn("crm", m)
        self.assertNotIn("mycrm", m)
        errs = connectors.load_errors()
        self.assertTrue(any(e.get("severity") == "error" and
                            "does not match its folder" in e["error"] for e in errs))

    def test_unmanageable_folder_name_loads_with_warning(self):
        _write(self.builtin, "MyApp", "label: Cased\n")
        ids = [m["id"] for m in self._load()]
        self.assertIn("MyApp", ids)  # visible — hiding it would be worse
        errs = connectors.load_errors()
        self.assertTrue(any("cannot be managed from Setup" in e["error"] for e in errs))

    def test_manifests_carry_their_source_dir(self):
        _write(self.user, "hasdir", "label: X\n")
        m = {x["id"]: x for x in self._load()}["hasdir"]
        self.assertTrue(m["_dir"].endswith(os.path.join("user", "hasdir")))


class NestedQuarantineTests(_RegistryCase):
    def test_mcp_env_compose_list_form_is_quarantined_not_fatal(self):
        _write(self.user, "hass", """\
            label: HA
            mcp:
              command: ["python3", "server.py"]
              env:
                - MYAPP_TOKEN=s3cret
        """)
        m = {x["id"]: x for x in self._load()}["hass"]
        self.assertNotIn("env", m.get("mcp", {}))
        self.assertIn("command", m.get("mcp", {}))
        errs = connectors.load_errors()
        self.assertTrue(any("mcp.env" in e["error"] for e in errs))

    def test_jobs_labels_wrong_shape_quarantined(self):
        _write(self.user, "worker", "label: W\njobs:\n  labels:\n    - a\n    - b\n")
        m = {x["id"]: x for x in self._load()}["worker"]
        self.assertNotIn("labels", m.get("jobs", {}))

    def test_quoted_ui_order_is_coerced(self):
        _write(self.user, "a-app", "label: A\nui:\n  label: A\n  order: '10'\n")
        m = {x["id"]: x for x in self._load()}["a-app"]
        self.assertEqual(m["ui"]["order"], 10)

    def test_garbage_ui_order_is_dropped_and_rail_survives(self):
        # One `order: high` used to TypeError the sort and blank the LEFT RAIL.
        _write(self.user, "a-app", "label: A\nui:\n  label: A\n  order: high\n")
        _write(self.user, "b-app", "label: B\nui:\n  label: B\n  order: 5\n")
        _write(self.user, "c-app", "label: C\nui:\n  label: C\n")
        self._load()  # apps() reads the cached registry — refresh it first
        rows = connectors.apps()
        self.assertEqual([a["id"] for a in rows], ["b-app", "a-app", "c-app"])
        errs = self._errors_after_load()
        self.assertTrue(any("ui.order" in e["error"] for e in errs))

    def test_apps_survives_even_unvalidated_garbage_order(self):
        self.assertEqual(connectors._safe_order("nope"), 100)
        self.assertEqual(connectors._safe_order(True), 100)
        self.assertEqual(connectors._safe_order("12"), 12)
        self.assertEqual(connectors._safe_order(None), 100)


class EgressShapeTests(_RegistryCase):
    def test_bare_string_hosts_and_routes_mean_one_entry_not_characters(self):
        # `hosts: "127.0.0.1:9002"` used to iterate per CHARACTER: 14 junk
        # endpoints and 84 blanket allow rules from one un-bracketed line.
        _write(self.user, "crm", """\
            label: CRM
            egress:
              routes: "POST /internal/connector/crm/create_lead"
              hosts: "127.0.0.1:9002"
        """)
        self._load()
        pol = connectors.render_egress_policy("crm")
        self.assertIsNotNone(pol)
        eps = list(pol["network_policies"].values())[0]["endpoints"]
        hosts = [e["host"] for e in eps]
        self.assertIn("127.0.0.1", hosts)
        self.assertNotIn("1", hosts)          # the per-character symptom
        self.assertNotIn("", hosts)
        bridge_rules = eps[0]["rules"]
        self.assertTrue(any(r["allow"]["path"].endswith("create_lead")
                            for r in bridge_rules))

    def test_numeric_egress_hosts_is_quarantined(self):
        _write(self.user, "oops", "label: O\negress:\n  hosts: 8482\n")
        self._load()
        pol = connectors.render_egress_policy("oops")
        self.assertIsNone(pol)  # nothing valid declared -> no policy, no crash
        errs = connectors.load_errors()
        self.assertTrue(any("egress.hosts" in e["error"] for e in errs))


class OrphanExtensionTests(_RegistryCase):
    def test_orphans_reports_stale_tool_files_grants_and_cache(self):
        _write(self.user, "app1", """\
            label: App1
            base_url: "http://127.0.0.1:9000"
            actions:
              - id: a1
                path: /a1
        """)
        tools_root = os.path.join(self.tmp, "tools")
        os.makedirs(os.path.join(tools_root, "app1"))
        os.makedirs(os.path.join(tools_root, "ghost"))
        for fn in ("app1_a1.mjs", "app1_old.mjs"):
            open(os.path.join(tools_root, "app1", fn), "w").write("//")
        open(os.path.join(tools_root, "ghost", "ghost_x.mjs"), "w").write("//")
        pol_dir = os.path.join(self.tmp, "pols")
        os.makedirs(pol_dir)
        open(os.path.join(pol_dir, "ghost.yaml"), "w").write("preset: {}\n")

        def tools_dir(cid=None):
            return os.path.join(tools_root, cid) if cid else tools_root

        with mock.patch.object(connectors.settings, "connector_tools_dir", tools_dir), \
             mock.patch.object(connectors.settings, "generated_policy_dir",
                               lambda: pol_dir), \
             mock.patch("ava_bridge.grants._load",
                        return_value={"app1": {}, "oldapp": {"x": {}}}), \
             mock.patch("ava_bridge.tools_cache._load",
                        return_value={"app1": {}, "staler": {}}):
            found = connectors.orphans()
        self.assertEqual(found["tools"], ["ghost"])
        self.assertEqual(found["policies"], ["ghost"])
        self.assertEqual(found["tool_files"], ["app1/app1_old.mjs"])
        self.assertEqual(found["grants"], ["oldapp"])
        self.assertEqual(found["cache"], ["staler"])


if __name__ == "__main__":
    unittest.main()


class EgressPolicyApplyShapeTests(_RegistryCase):
    def test_host_endpoints_never_carry_allowed_ips(self):
        # NemoClaw's policy-add rejects `allowed_ips` on any endpoint other
        # than the bridge one — a generated policy carrying it on an
        # `egress.hosts` entry fails to apply wholesale and the connector's
        # tools hit deny-by-default (found wiring an owner app's sidecar).
        _write(self.user, "sidecar", """\
            label: Sidecar
            egress:
              hosts: ["127.0.0.1:9310"]
        """)
        connectors.load(force=True)
        pol = connectors.render_egress_policy("sidecar")
        eps = list(pol["network_policies"].values())[0]["endpoints"]
        host_eps = [e for e in eps if e["host"] != "host.openshell.internal"]
        self.assertTrue(host_eps, "expected an egress.hosts endpoint")
        for ep in host_eps:
            self.assertNotIn("allowed_ips", ep,
                             "policy-add refuses allowed_ips on user endpoints")
