"""Three gaps in the owner's control over a connector.

1. NO OFF SWITCH FOR A BUILT-IN. `POST /connectors/<cid>/enabled` refused any
   connector without a user manifest, on the correct principle that Ava never
   rewrites the owner's shipped YAML. The consequence was that a built-in
   connector able to spend money had no UI off switch at all. `_merge_all` already
   resolves the user root over the built-in root by id, so a two-line override stub
   disables one without touching the shipped file — no new mechanism.

   The stub is refused when both roots are the SAME directory (AVA_HOME unset ->
   code root), because there the stub would overwrite the manifest it means to
   shadow. That collapse is real on a single-box install, and is why a fleet keeps
   AVA_HOME outside the checkout.

2. NO TIER FOR "READS PRIVATE DATA". `read` meant "no side effects" and was
   implemented as "runs silently, forever". An author wanting "ask before you read
   my conversations" had to label it `write` (a lie about side effects) or
   `destructive` (a lie that also trains the owner to tap through the prompt that
   matters). `sensitive` is enforcement-identical to `write` and truthful.

3. OPENAPI ACTIONS COULD NOT TAKE ARGUMENTS. `_actions_from_openapi` emitted no
   `input:`, so `render_tool` declared `additionalProperties: false` with no
   properties and a templated path was requested with the literal `{id}` in it.
   Asymmetric, too: at >= 16 actions the meta-tool path omits
   `additionalProperties`, so arguments worked — the same manifest behaved
   differently either side of META_TOOLS_MIN.
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ava_bridge import connectors
from ava_bridge.hub.connectors import _actions_from_openapi, set_connector_enabled


def _with(manifest):
    return mock.patch.object(connectors, "load", return_value=[manifest])


class SensitiveTierTests(unittest.TestCase):
    M = {"id": "mail", "mcp": {"url": "http://127.0.0.1:9200/mcp"},
         "dynamic_access": {"read_inbox": "sensitive", "get_*": "read", "*": "write"}}

    def test_it_is_a_recognised_tier(self):
        self.assertIn("sensitive", connectors._TIERS)

    def test_it_asks(self):
        with _with(self.M), mock.patch("ava_bridge.grants.has", return_value=False):
            self.assertTrue(connectors.needs_confirm("mail", "read_inbox"))

    def test_it_is_grantable_unlike_destructive(self):
        """The point of the tier: it can be always-allowed, so it does not train
        the owner to tap through prompts."""
        with _with(self.M):
            self.assertTrue(connectors.grantable("mail", "read_inbox"))

    def test_a_grant_silences_it_like_write(self):
        with _with(self.M), mock.patch("ava_bridge.grants.has", return_value=True):
            self.assertFalse(connectors.needs_confirm("mail", "read_inbox"))

    def test_plain_read_is_still_silent(self):
        with _with(self.M):
            self.assertFalse(connectors.needs_confirm("mail", "get_status"))

    def test_the_tier_reaches_the_approval_record(self):
        """approvals.gate puts action_access on the pending row, so the prompt can
        say what it is asking about without new plumbing."""
        with _with(self.M):
            self.assertEqual(connectors.action_access("mail", "read_inbox"),
                             "sensitive")

    def test_destructive_is_still_never_grantable(self):
        m = dict(self.M, dynamic_access={"wipe": "destructive", "*": "write"})
        with _with(m):
            self.assertFalse(connectors.grantable("mail", "wipe"))

    def test_the_tools_cache_can_represent_every_tier(self):
        """It was short by two, so a device tool self-reporting `physical` was
        stored and enforced as the weaker `write`."""
        from ava_bridge import tools_cache
        self.assertEqual(set(connectors._TIERS), set(tools_cache._TIERS))


class BuiltinOffSwitchTests(unittest.TestCase):
    BUILTIN = {"id": "spendy", "label": "Spendy", "kind": "app", "enabled": True}

    def setUp(self):
        self.builtin_dir = tempfile.mkdtemp()
        self.user_dir = tempfile.mkdtemp()
        self._p = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.builtin_dir),
            mock.patch.object(connectors, "USER_DIR", self.user_dir),
            mock.patch.object(connectors, "catalog", return_value=[self.BUILTIN]),
            mock.patch("ava_bridge.hub.connectors.perf_mgmt.refresh_sources"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in reversed(self._p):
            p.stop()
        connectors.load(force=True)

    def test_disabling_a_builtin_writes_an_override_stub(self):
        import yaml
        out = set_connector_enabled("spendy", {"enabled": False})
        self.assertTrue(out["ok"])
        self.assertTrue(out["stub"])
        p = os.path.join(self.user_dir, "spendy", "connector.yaml")
        self.assertTrue(os.path.isfile(p))
        m = yaml.safe_load(open(p, encoding="utf-8"))
        self.assertEqual(m, {"id": "spendy", "enabled": False})

    def test_the_stub_actually_disables_it_through_the_loader(self):
        import yaml
        os.makedirs(os.path.join(self.builtin_dir, "spendy"), exist_ok=True)
        with open(os.path.join(self.builtin_dir, "spendy", "connector.yaml"), "w") as f:
            yaml.safe_dump(self.BUILTIN, f)
        self.assertIn("spendy", [x["id"] for x in connectors.load(force=True)])
        set_connector_enabled("spendy", {"enabled": False})
        self.assertNotIn("spendy", [x["id"] for x in connectors.load(force=True)])

    def test_the_shipped_manifest_is_never_rewritten(self):
        import yaml
        os.makedirs(os.path.join(self.builtin_dir, "spendy"), exist_ok=True)
        shipped = os.path.join(self.builtin_dir, "spendy", "connector.yaml")
        with open(shipped, "w") as f:
            yaml.safe_dump(self.BUILTIN, f)
        before = open(shipped, encoding="utf-8").read()
        set_connector_enabled("spendy", {"enabled": False})
        self.assertEqual(open(shipped, encoding="utf-8").read(), before)

    def test_enabling_a_builtin_with_no_stub_writes_nothing(self):
        out = set_connector_enabled("spendy", {"enabled": True})
        self.assertTrue(out["ok"])
        self.assertFalse(out["stub"])
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "spendy")))

    def test_an_unknown_connector_is_still_404(self):
        r = set_connector_enabled("nope", {"enabled": False})
        self.assertEqual(r.status_code, 404)


class CollapsedRootsTests(unittest.TestCase):
    """AVA_HOME unset -> USER_DIR == BUILTIN_DIR. The stub must be refused there,
    or it would overwrite the very manifest it is meant to shadow."""

    def test_a_stub_is_refused_when_both_roots_are_one_directory(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(connectors, "BUILTIN_DIR", d), \
                mock.patch.object(connectors, "USER_DIR", d), \
                mock.patch.object(connectors, "catalog",
                                  return_value=[{"id": "spendy"}]):
            r = set_connector_enabled("spendy", {"enabled": False})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(os.path.exists(os.path.join(d, "spendy")))


class OpenApiInputTests(unittest.TestCase):
    SPEC = {"paths": {
        "/api/items/{id}": {
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string"}}],
            "get": {"summary": "Get item",
                    "parameters": [{"name": "verbose", "in": "query",
                                    "schema": {"type": "boolean"}}]},
            "post": {"summary": "Update item", "requestBody": {"content": {
                "application/json": {"schema": {
                    "type": "object", "required": ["name"],
                    "properties": {"name": {"type": "string"},
                                   "count": {"type": "integer"},
                                   "nested": {"type": "object"}}}}}}},
        },
        "/api/ping": {"get": {"summary": "Ping"}},
    }}

    def _by_id(self):
        return {a["id"]: a for a in _actions_from_openapi(self.SPEC)}

    def test_a_path_param_becomes_a_required_input(self):
        a = self._by_id()["get_items"]
        self.assertIn("id", a["input"]["properties"])
        self.assertIn("id", a["input"]["required"])

    def test_path_level_params_apply_to_every_operation(self):
        """`parameters` on the path item, not the operation — per the spec."""
        self.assertIn("id", self._by_id()["post_items"]["input"]["properties"])

    def test_a_query_param_keeps_its_type(self):
        props = self._by_id()["get_items"]["input"]["properties"]
        self.assertEqual(props["verbose"]["type"], "boolean")

    def test_request_body_properties_are_surfaced(self):
        props = self._by_id()["post_items"]["input"]["properties"]
        self.assertEqual(props["name"]["type"], "string")
        self.assertEqual(props["count"]["type"], "integer")

    def test_nested_objects_are_skipped(self):
        """The generated tool sends GET args as query params and POST as a flat
        JSON body; a nested object would not round-trip."""
        self.assertNotIn("nested", self._by_id()["post_items"]["input"]["properties"])

    def test_an_operation_with_no_params_gets_no_input_key(self):
        self.assertNotIn("input", self._by_id()["get_ping"])

    def test_the_generated_tool_no_longer_forbids_all_arguments(self):
        m = {"id": "acme", "base_url": "http://127.0.0.1:9000",
             "actions": [self._by_id()["get_items"]]}
        with _with(m):
            src = connectors.render_tool("acme", self._by_id()["get_items"])
        self.assertIn("id", src)
        self.assertNotIn('"additionalProperties": false, "properties": {}', src)


if __name__ == "__main__":
    unittest.main()


class SandboxPersistenceTests(unittest.TestCase):
    """A command PROBED in a container must not be SAVED to run outside one.

    `_probe` defaults to Docker and fails closed; the manifest writer only wrote
    `sandbox: docker` when the client explicitly said so, and `mcp_client` reads
    an absent key as uncontained (`spec.get("sandbox") or "none"`). The frontend
    sends the flag from `dockerAvail`, which is stuck at `true` when
    `hub.system()` fails — so the two halves could disagree and hand an arbitrary
    pasted command the bridge's whole environment. Containment defaults ON now,
    and dropping it is an explicit `sandbox: none` in the request and in the diff.
    """

    def _manifest(self, mcp_in: dict) -> dict:
        from ava_bridge.hub import connectors as hub_connectors
        written = {}

        async def _run():
            with mock.patch.object(hub_connectors.settings, "home",
                                   side_effect=lambda *p: os.path.join(self.tmp, *p)), \
                 mock.patch.object(hub_connectors.connectors, "load"), \
                 mock.patch.object(hub_connectors.connectors, "load_errors",
                                   return_value=[]), \
                 mock.patch.object(hub_connectors.perf_mgmt, "refresh_sources"), \
                 mock.patch.object(hub_connectors.settings, "set_env_secret"):
                r = await hub_connectors.connector_new(
                    {"id": "acme", "label": "Acme", "mcp": mcp_in})
            written.update(r.get("manifest") or {})

        asyncio.run(_run())
        return written

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_stdio_command_is_contained_by_default(self):
        m = self._manifest({"command": "npx -y some-server"})
        self.assertEqual(m["mcp"]["sandbox"], "docker",
                         "an absent flag reads as UNCONTAINED downstream")

    def test_an_explicit_opt_out_is_recorded_as_such(self):
        m = self._manifest({"command": "npx -y some-server", "sandbox": "none"})
        self.assertEqual(m["mcp"]["sandbox"], "none")

    def test_docker_stays_docker(self):
        m = self._manifest({"command": "npx -y some-server", "sandbox": "docker"})
        self.assertEqual(m["mcp"]["sandbox"], "docker")

    def test_a_url_server_has_no_sandbox_key_at_all(self):
        """Containment is about a process WE spawn. An HTTP endpoint is someone
        else's process and there is nothing here to contain."""
        m = self._manifest({"url": "http://127.0.0.1:9000/mcp"})
        self.assertNotIn("sandbox", m["mcp"])
