"""Nothing ever asked "what is here that should NOT be?".

Every desired-vs-actual comparison in the codebase walks what the manifests
declare and looks each item up in what the sandbox holds. None walked the other
way. Two consequences, both observed on a real install:

  * Generated tools outlived their connector. `install.sh` tars the whole
    `apps/` tree, so on the next provision they ship into the sandbox with no
    egress policy allowing their `/internal/connector/<cid>/*` routes — which
    deny-by-default guarantees will fail. Ava is handed tools that cannot work,
    and no surface said so.
  * Deleting a connector left its standing grants, its saved credential, its
    self-reported tier cache and its running stdio subprocess behind. Connector
    ids are reusable, so the next app to take the name inherited all of it —
    including "Always allow" for actions a DIFFERENT app was granted.

House style: stdlib unittest, real temp dirs, no bridge, no network.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors, grants, settings, tools_cache


def _write_manifest(base: str, cid: str, body: str) -> None:
    d = os.path.join(base, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


class OrphanTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ava-orphan-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.manifests = os.path.join(self.tmp, "connectors")
        self.state = os.path.join(self.tmp, "agent")
        os.makedirs(self.manifests)
        self.tools = os.path.join(self.state, "mcp_server_connectors", "apps")
        self.policies = os.path.join(self.state, "policies", "generated")
        os.makedirs(self.tools)
        os.makedirs(self.policies)

        for p in (
            mock.patch.object(connectors, "BUILTIN_DIR", self.manifests),
            mock.patch.object(connectors, "USER_DIR", self.manifests),
            mock.patch.object(connectors.settings, "agent_state_dir",
                              return_value=self.state),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _tool(self, cid: str) -> str:
        d = os.path.join(self.tools, cid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{cid}_call.mjs"), "w", encoding="utf-8") as f:
            f.write("// generated\n")
        return d

    def _policy(self, cid: str) -> str:
        p = os.path.join(self.policies, f"{cid}.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("preset:\n  name: ava-%s\n" % cid)
        return p

    def test_generated_files_with_no_manifest_are_orphans(self):
        _write_manifest(self.manifests, "kept", "id: kept\nlabel: Kept\n")
        connectors.load(force=True)
        self._tool("kept")
        self._tool("gone")
        self._policy("gone")

        found = connectors.orphans()
        self.assertEqual(found["tools"], ["gone"])
        self.assertEqual(found["policies"], ["gone"])

    def test_a_disabled_connector_is_dormant_not_orphaned(self):
        """It still has a manifest. Pruning its tools would delete work the owner
        expects to get back by flipping the switch."""
        _write_manifest(self.manifests, "paused",
                        "id: paused\nlabel: Paused\nenabled: false\n")
        connectors.load(force=True)
        self._tool("paused")
        self._policy("paused")

        found = connectors.orphans()
        self.assertEqual(found["tools"], [])
        self.assertEqual(found["policies"], [])

    def test_prune_reports_without_touching_by_default(self):
        connectors.load(force=True)
        d = self._tool("gone")
        p = self._policy("gone")

        planned = connectors.prune_orphans()
        self.assertEqual(planned["tools"], ["gone"])
        self.assertTrue(os.path.isdir(d), "a dry run deleted the tools")
        self.assertTrue(os.path.isfile(p), "a dry run deleted the policy")

    def test_prune_write_removes_them_and_audits(self):
        connectors.load(force=True)
        d = self._tool("gone")
        p = self._policy("gone")

        with mock.patch("ava_bridge.audit.record") as rec:
            removed = connectors.prune_orphans(write=True)

        self.assertEqual(removed["tools"], ["gone"])
        self.assertEqual(removed["policies"], ["gone"])
        self.assertFalse(os.path.exists(d))
        self.assertFalse(os.path.exists(p))
        rec.assert_called_once()
        self.assertEqual(rec.call_args.args[0], "connector_prune",
                         "removing an egress policy has to reach the ledger")

    def test_prune_with_nothing_to_do_writes_no_ledger_row(self):
        _write_manifest(self.manifests, "kept", "id: kept\nlabel: Kept\n")
        connectors.load(force=True)
        self._tool("kept")
        with mock.patch("ava_bridge.audit.record") as rec:
            connectors.prune_orphans(write=True)
        rec.assert_not_called()


class DeleteResidueTests(unittest.TestCase):
    """`delete_connector`'s purge, at the module level it actually lives in."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ava-residue-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for mod, attr, val in (
            (grants, "PATH", os.path.join(self.tmp, "connector_grants.yaml")),
            (tools_cache, "PATH", os.path.join(self.tmp, "tools_cache.json")),
        ):
            p = mock.patch.object(mod, attr, val)
            p.start()
            self.addCleanup(p.stop)
        grants._cache.update(data=None, mtime=0.0)
        tools_cache._cache.update(data=None, mtime=0.0)

    def test_forgetting_grants_removes_every_action(self):
        grants.grant("acme", "create_note")
        grants.grant("acme", "delete_note")
        grants.grant("other", "read")

        gone = grants.forget("acme")

        self.assertEqual(gone, ["create_note", "delete_note"])
        self.assertFalse(grants.has("acme", "create_note"))
        self.assertTrue(grants.has("other", "read"),
                        "forgetting one connector took another's grants with it")

    def test_a_reused_id_does_not_inherit_the_old_apps_permissions(self):
        """The reason this matters. Ids are reusable and the file is keyed on
        them, so without a purge the next app called `acme` starts out already
        allowed to do whatever the last one was."""
        grants.grant("acme", "delete_everything")
        grants.forget("acme")
        self.assertFalse(grants.has("acme", "delete_everything"))

    def test_forgetting_grants_audits_each_revocation(self):
        grants.grant("acme", "create_note")
        with mock.patch("ava_bridge.audit.record") as rec:
            grants.forget("acme")
        self.assertEqual(rec.call_args.args[0], "revoke")
        self.assertEqual(rec.call_args.kwargs["action"], "create_note",
                         "the ledger must be able to answer WHEN Ava stopped "
                         "being allowed to do a specific thing")

    def test_forgetting_nothing_is_not_an_event(self):
        with mock.patch("ava_bridge.audit.record") as rec:
            self.assertEqual(grants.forget("never-existed"), [])
        rec.assert_not_called()

    def test_the_tier_cache_forgets_too(self):
        tools_cache.update("acme", [{"name": "wipe", "access": "destructive"}])
        self.assertEqual(tools_cache.access("acme", "wipe"), "destructive")

        self.assertEqual(tools_cache.forget("acme"), 1)
        self.assertIsNone(tools_cache.access("acme", "wipe"),
                          "a deleted app's self-reported tiers survived, and they "
                          "are what decides which actions run silently")
        self.assertEqual(tools_cache.forget("acme"), 0)


class DeleteRouteTests(unittest.TestCase):
    """The route wires all of it together, including the MCP session."""

    def test_delete_purges_grants_credential_cache_and_session(self):
        from ava_bridge.hub import connectors as hub_connectors

        tmp = tempfile.mkdtemp(prefix="ava-delete-")
        self.addCleanup(shutil.rmtree, tmp, True)
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, "connectors", "acme"))
        with open(os.path.join(home, "connectors", "acme", "connector.yaml"),
                  "w", encoding="utf-8") as f:
            f.write("id: acme\nlabel: Acme\nauth:\n  token_env: ACME_TOKEN\n")

        manifest = {"id": "acme", "label": "Acme",
                    "auth": {"token_env": "ACME_TOKEN"}}
        with mock.patch.object(settings, "home",
                               side_effect=lambda *p: os.path.join(home, *p)), \
             mock.patch.object(settings, "connector_tools_dir",
                               return_value=os.path.join(tmp, "tools", "acme")), \
             mock.patch.object(settings, "generated_policy_dir",
                               return_value=os.path.join(tmp, "pol")), \
             mock.patch.object(connectors, "all", return_value=[manifest]), \
             mock.patch.object(connectors, "load"), \
             mock.patch.object(hub_connectors.perf_mgmt, "refresh_sources"), \
             mock.patch.object(hub_connectors.grants, "forget",
                               return_value=["create"]) as forget, \
             mock.patch.object(hub_connectors.tools_cache, "forget",
                               return_value=2) as cache_forget, \
             mock.patch.object(hub_connectors.mcp_client, "reset") as reset, \
             mock.patch.object(settings, "clear_env_secret") as clear, \
             mock.patch("ava_bridge.audit.record"):
            res = hub_connectors.delete_connector("acme")

        self.assertTrue(res["ok"], res)
        forget.assert_called_once_with("acme")
        cache_forget.assert_called_once_with("acme")
        reset.assert_called_once_with("acme")
        clear.assert_called_once_with("ACME_TOKEN")

    def test_the_response_does_not_imply_the_sandbox_allowance_is_gone(self):
        """`policy-add` has no remove verb. The file going is not the allowance
        going, and the route must not let the UI say otherwise."""
        import inspect
        src = inspect.getsource(
            __import__("ava_bridge.hub.connectors", fromlist=["x"]).delete_connector)
        self.assertIn("policy_still_in_sandbox", src)


if __name__ == "__main__":
    unittest.main()
