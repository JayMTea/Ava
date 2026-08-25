"""The whole life of a connected app survives its worst manifests.

Regression suite for the 2026-08 connector audit, hub-route half. The loader
half (tests/test_connector_validation_depth.py) proves one bad manifest never
takes down the registry; this file proves the Setup routes built ON that
registry stay honest and manageable around the same failures:

  * a broken manifest's row still carries real `builtin`/`enabled` plus the
    error — the old fallback hard-coded builtin=True, stripping Edit/Remove/
    Disable from exactly the rows that needed them;
  * "already exists" means A MANIFEST EXISTS, not "one is enabled and parses" —
    connector_new used to overwrite a disabled or mid-edit manifest;
  * the OpenAPI `input:` schema survives create (it was dropped, so every
    detected tool was generated argument-less);
  * generate --write prunes a renamed action's stale .mjs (install.sh tars the
    whole dir, so stale files ship into the sandbox as phantom tools);
  * disabling or editing an MCP connector resets its live stdio session;
  * a manifest save is atomic and returns the loader's validation verdict.

House style: stdlib unittest + fastapi TestClient, real temp dirs, no network.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava_bridge import connectors, settings
from ava_bridge.hub import connectors as hub

app = FastAPI()
app.include_router(hub.router, prefix="/api/hub")


def _write(base: str, cid: str, body: str) -> None:
    d = os.path.join(base, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


class _HubCase(unittest.TestCase):
    """Hub routes against a throwaway AVA_HOME-shaped tree.

    BUILTIN_DIR/USER_DIR are patched on the registry (the same harness as
    test_connectors.py) and settings.home / agent_state_dir are pointed at the
    same tree, so the routes' own path math (delete, connector_new, generate)
    agrees with the loader about where everything lives.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ava-lifecycle-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.user = os.path.join(self.home, "connectors")
        self.builtin = os.path.join(self.tmp, "builtin")
        self.state = os.path.join(self.home, "agent")
        os.makedirs(self.user)
        os.makedirs(self.builtin)

        self.mcp_reset = mock.MagicMock()
        self.grants_forget = mock.MagicMock(return_value=[])
        self.cache_forget = mock.MagicMock(return_value=0)
        for p in (
            mock.patch.object(connectors, "BUILTIN_DIR", self.builtin),
            mock.patch.object(connectors, "USER_DIR", self.user),
            mock.patch.object(settings, "home",
                              side_effect=lambda *p: os.path.join(self.home, *p)),
            mock.patch.object(settings, "AVA_HOME", Path(self.home)),
            mock.patch.object(settings, "agent_state_dir", return_value=self.state),
            mock.patch.object(hub.perf_mgmt, "refresh_sources"),
            mock.patch.object(hub.audit, "record"),
            mock.patch.object(hub.mcp_client, "reset", self.mcp_reset),
            mock.patch.object(hub.grants, "forget", self.grants_forget),
            mock.patch.object(hub.tools_cache, "forget", self.cache_forget),
        ):
            p.start()
            self.addCleanup(p.stop)
        connectors.load(force=True)
        self.addCleanup(connectors.load, True)  # don't leak this tree's registry
        self.client = TestClient(app)

    def _rows(self) -> dict:
        r = self.client.get("/api/hub/connectors")
        self.assertEqual(r.status_code, 200)
        return {x["id"]: x for x in r.json()["connectors"]}


class BrokenManifestRowTests(_HubCase):
    """One bad field must not cost the owner the buttons that fix it."""

    BROKEN = """\
    id: shop
    label: Shop
    egress:
      routes: {oops: 1}
    """

    def test_quarantined_manifest_yields_a_manageable_row_with_the_error(self):
        _write(self.user, "shop", self.BROKEN)
        connectors.load(force=True)
        row = self._rows()["shop"]
        self.assertFalse(row["builtin"], "a user manifest read as built-in "
                         "hides Edit/Disable/Remove exactly when they're needed")
        self.assertTrue(row["enabled"])
        self.assertIn("egress", row.get("error") or "",
                      "the loader's quarantine message must reach the row")

    def test_a_row_builder_crash_still_yields_a_manageable_row(self):
        """The guarantee for fields _validate does not yet know about."""
        _write(self.user, "crashy", "id: crashy\nlabel: Crashy\n")
        connectors.load(force=True)
        with mock.patch.object(hub, "_connector_row",
                               side_effect=RuntimeError("boom")):
            row = self._rows()["crashy"]
        self.assertEqual(row["error"], "boom")
        self.assertFalse(row["builtin"],
                         "the fallback used to hard-code builtin=True")
        self.assertTrue(row["enabled"],
                        "the fallback used to hard-code enabled=False")

    def test_the_broken_connector_can_still_be_deleted(self):
        _write(self.user, "shop", self.BROKEN)
        connectors.load(force=True)
        r = self.client.post("/api/hub/connectors/shop/delete")
        self.assertTrue(r.json()["ok"], r.json())
        self.assertFalse(os.path.isdir(os.path.join(self.user, "shop")))

    def test_an_unparsable_manifest_can_still_be_deleted(self):
        """catalog() never sees this one at all — delete is folder-keyed."""
        d = os.path.join(self.user, "junk")
        os.makedirs(d)
        with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
            f.write("{{{ not yaml")
        connectors.load(force=True)
        r = self.client.post("/api/hub/connectors/junk/delete")
        self.assertTrue(r.json()["ok"], r.json())
        self.assertFalse(os.path.isdir(d))


class ConnectorNewTests(_HubCase):
    """`/connectors/new` must refuse every EXISTING manifest, and a fresh id
    must not inherit a manually-deleted predecessor's state."""

    def test_409_on_a_disabled_id(self):
        mine = "id: notes\nlabel: Mine\nenabled: false\n"
        _write(self.user, "notes", mine)
        connectors.load(force=True)
        r = self.client.post("/api/hub/connectors/new", json={"id": "notes"})
        self.assertEqual(r.status_code, 409,
                         "a disabled manifest is still the owner's manifest")
        with open(os.path.join(self.user, "notes", "connector.yaml"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), mine, "the 409 must leave the file alone")

    def test_409_on_an_unparsable_manifest_id(self):
        broken = "# my work\nid: notes\nactions: [unclosed\n"
        d = os.path.join(self.user, "notes")
        os.makedirs(d)
        with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
            f.write(broken)
        connectors.load(force=True)
        r = self.client.post("/api/hub/connectors/new", json={"id": "notes"})
        self.assertEqual(r.status_code, 409,
                         "a mid-edit manifest the loader quarantined is not "
                         "an invitation to overwrite it")
        with open(os.path.join(d, "connector.yaml"), encoding="utf-8") as f:
            self.assertEqual(f.read(), broken)

    def test_input_schema_survives_into_the_written_manifest(self):
        act = {"id": "get_item", "method": "GET", "path": "/api/items/{id}",
               "description": "Get one item", "access": "read",
               "input": {"properties": {"id": {"type": "string"}},
                         "required": ["id"]}}
        r = self.client.post("/api/hub/connectors/new",
                             json={"id": "myapp",
                                   "base_url": "http://127.0.0.1:9000",
                                   "actions": [act]})
        self.assertTrue(r.json()["ok"], r.json())
        with open(os.path.join(self.user, "myapp", "connector.yaml"),
                  encoding="utf-8") as f:
            saved = yaml.safe_load(f.read())
        a = saved["actions"][0]
        self.assertEqual(a["input"]["properties"]["id"]["type"], "string",
                         "dropping `input` generates a tool that refuses "
                         "arguments — {id} stays literal in the path")
        self.assertEqual(a["input"]["required"], ["id"])

    def test_a_successful_create_clears_inherited_per_id_state(self):
        r = self.client.post("/api/hub/connectors/new", json={"id": "fresh"})
        self.assertTrue(r.json()["ok"], r.json())
        self.grants_forget.assert_any_call("fresh")
        self.cache_forget.assert_any_call("fresh")

    def test_a_refused_create_clears_nothing(self):
        _write(self.user, "notes", "id: notes\nenabled: false\n")
        connectors.load(force=True)
        self.client.post("/api/hub/connectors/new", json={"id": "notes"})
        self.grants_forget.assert_not_called()
        self.cache_forget.assert_not_called()


class GeneratePruneTests(_HubCase):
    MANIFEST = """\
    id: acme
    label: Acme
    base_url: http://127.0.0.1:9000
    egress:
      hosts: ["127.0.0.1:9000"]
    actions:
      - {id: %s, path: /a, method: GET, description: does a thing}
    """

    def test_write_prunes_a_renamed_actions_stale_tool(self):
        _write(self.user, "acme", self.MANIFEST % "old_one")
        connectors.load(force=True)
        r1 = self.client.post("/api/hub/connectors/acme/generate?write=1").json()
        tdir = settings.connector_tools_dir("acme")
        self.assertTrue(os.path.exists(os.path.join(tdir, "acme_old_one.mjs")), r1)

        _write(self.user, "acme", self.MANIFEST % "new_one")
        connectors.load(force=True)
        r2 = self.client.post("/api/hub/connectors/acme/generate?write=1").json()
        self.assertTrue(os.path.exists(os.path.join(tdir, "acme_new_one.mjs")))
        self.assertFalse(os.path.exists(os.path.join(tdir, "acme_old_one.mjs")),
                         "install.sh tars the whole dir — the stale render "
                         "ships as a phantom tool whose route the policy refuses")
        self.assertTrue(any(p.endswith("acme_old_one.mjs")
                            for p in r2.get("pruned", [])),
                        "what was pruned must be reported, not silent")


class EnableDisableTests(_HubCase):
    MANIFEST = """\
    id: mailer
    label: Mailer
    enabled: true
    mcp:
      command: [npx, -y, some-server]
    """

    def test_disable_resets_the_live_mcp_session(self):
        _write(self.user, "mailer", self.MANIFEST)
        connectors.load(force=True)
        r = self.client.post("/api/hub/connectors/mailer/enabled",
                             json={"enabled": False})
        self.assertTrue(r.json()["ok"], r.json())
        self.assertFalse(r.json()["enabled"])
        self.mcp_reset.assert_called_once_with("mailer")

    def test_enable_does_not_reset(self):
        """Enable spawns nothing by itself; the next call does — nothing to kill."""
        _write(self.user, "mailer", self.MANIFEST.replace("enabled: true",
                                                          "enabled: false"))
        connectors.load(force=True)
        r = self.client.post("/api/hub/connectors/mailer/enabled",
                             json={"enabled": True})
        self.assertTrue(r.json()["ok"], r.json())
        self.mcp_reset.assert_not_called()


class ManifestSaveTests(_HubCase):
    GOOD = "id: shop\nlabel: Shop\n"

    def setUp(self) -> None:
        super().setUp()
        _write(self.user, "shop", self.GOOD)
        connectors.load(force=True)

    def test_save_goes_through_atomic_write_and_lands(self):
        real = settings.atomic_write
        body = "id: shop\nlabel: Renamed\n"
        with mock.patch.object(settings, "atomic_write",
                               side_effect=real) as aw:
            r = self.client.post("/api/hub/connectors/shop/manifest",
                                 json={"yaml": body})
        self.assertTrue(r.json()["ok"], r.json())
        aw.assert_called_once()
        self.assertTrue(str(aw.call_args.args[0]).endswith("connector.yaml"))
        with open(os.path.join(self.user, "shop", "connector.yaml"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), body)

    def test_save_returns_the_loaders_verdict_for_bad_but_parsable_yaml(self):
        bad = "id: shop\negress:\n  routes: {oops: 1}\n"
        r = self.client.post("/api/hub/connectors/shop/manifest",
                             json={"yaml": bad}).json()
        self.assertTrue(r["ok"], "the save is real — that part is true")
        self.assertTrue(r.get("errors"),
                        "…but the owner pressing Save is exactly who needs "
                        "the validation verdict")
        self.assertIn("egress", r["errors"][0]["error"])

    def test_save_resets_the_mcp_session(self):
        """An edit may change the command or credential — the old child dies."""
        self.client.post("/api/hub/connectors/shop/manifest",
                         json={"yaml": "id: shop\nlabel: Edited\n"})
        self.mcp_reset.assert_called_with("shop")

    def test_unparsable_yaml_is_rejected_and_never_written(self):
        r = self.client.post("/api/hub/connectors/shop/manifest",
                             json={"yaml": "{{{ nope"})
        self.assertEqual(r.status_code, 400)
        with open(os.path.join(self.user, "shop", "connector.yaml"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), self.GOOD)


class CliIconTests(unittest.TestCase):
    """`ava connector apps` on the documented default (no declared icon)."""

    def test_a_none_icon_formats_instead_of_typeerroring(self):
        import ava_cli
        rows = [{"id": "myapp", "section": "apps", "embed": "iframe",
                 "icon": None, "url": "http://127.0.0.1:9000", "view": None}]
        buf = io.StringIO()
        with mock.patch.object(connectors, "apps", return_value=rows), \
             contextlib.redirect_stdout(buf):
            rc = ava_cli.cmd_connector(
                argparse.Namespace(action="apps", name=None, write=False))
        self.assertEqual(rc, 0, "f'{None:10}' is a TypeError — the listing "
                         "crashed on precisely the recommended manifest shape")
        self.assertIn("icon=-", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
