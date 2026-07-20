"""Connected-app rail identity: the auto-varying icon + the Hub's appearance
picker endpoint.

Background: every app the user added rendered with the SAME grid glyph, because
apps() hard-coded `or "grid"` as the icon fallback. Color already auto-varied
(appAccent hashes the id), so only the dot distinguished two apps. The fix is
symmetric with color — the backend reports the icon as a FACT (None when the
manifest doesn't declare one) and the frontend's appIcon() picks a stable glyph
from the id — plus an endpoint so the user can override either one.
"""
import json
import os
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors, hub_api


def _write(base, cid, body):
    d = os.path.join(base, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


def _read(base, cid):
    import yaml
    with open(os.path.join(base, cid, "connector.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _body(resp):
    """The decoded JSON of a JSONResponse (error paths return one)."""
    return json.loads(bytes(resp.body).decode("utf-8"))


class AppIconFactTests(unittest.TestCase):
    """apps() must report the manifest fact, never a presentation default."""

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

    def test_undeclared_icon_is_null_not_grid(self):
        # REGRESSION: "grid" here made every undeclared app look identical in
        # the rail. None lets the frontend vary it stably per app id.
        _write(self.user, "myapp", """\
            id: myapp
            label: My App
            ui: { embed: iframe, url: "http://127.0.0.1:9" }
        """)
        connectors.load(force=True)
        app = next(a for a in connectors.apps() if a["id"] == "myapp")
        self.assertIsNone(app["icon"])

    def test_declared_icon_and_color_pass_through(self):
        _write(self.user, "myapp", """\
            id: myapp
            label: My App
            ui: { embed: iframe, url: "http://127.0.0.1:9", icon: cloud, color: "#ff8800" }
        """)
        connectors.load(force=True)
        app = next(a for a in connectors.apps() if a["id"] == "myapp")
        self.assertEqual(app["icon"], "cloud")
        self.assertEqual(app["color"], "#ff8800")


class AppearanceEndpointTests(unittest.TestCase):
    """POST /api/hub/connectors/{cid}/appearance — writes ui.icon / ui.color."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.user = os.path.join(self.tmp, "connectors")
        self.builtin = os.path.join(self.tmp, "builtin")
        os.makedirs(self.user)
        os.makedirs(self.builtin)
        self._p = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.builtin),
            mock.patch.object(connectors, "USER_DIR", self.user),
            mock.patch.object(hub_api.settings, "home",
                              side_effect=lambda *p: os.path.join(self.tmp, *p)),
        ]
        for p in self._p:
            p.start()
        _write(self.user, "myapp", """\
            id: myapp
            label: My App
            ui: { embed: iframe, url: "http://127.0.0.1:9" }
        """)
        connectors.load(force=True)

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_sets_icon_and_color(self):
        r = hub_api.set_connector_appearance(
            "myapp", {"icon": "cloud", "color": "var(--app-accent-3)"})
        self.assertTrue(r["ok"])
        ui = _read(self.user, "myapp")["ui"]
        self.assertEqual(ui["icon"], "cloud")
        self.assertEqual(ui["color"], "var(--app-accent-3)")
        # And it reaches the rail payload without a restart.
        app = next(a for a in connectors.apps() if a["id"] == "myapp")
        self.assertEqual(app["icon"], "cloud")

    def test_patch_is_partial(self):
        # Setting only the color must not disturb an existing icon.
        hub_api.set_connector_appearance("myapp", {"icon": "db"})
        hub_api.set_connector_appearance("myapp", {"color": "#abcdef"})
        ui = _read(self.user, "myapp")["ui"]
        self.assertEqual(ui["icon"], "db")
        self.assertEqual(ui["color"], "#abcdef")

    def test_null_clears_back_to_auto(self):
        hub_api.set_connector_appearance("myapp", {"icon": "db", "color": "#abcdef"})
        r = hub_api.set_connector_appearance("myapp", {"icon": None, "color": None})
        self.assertTrue(r["ok"])
        ui = _read(self.user, "myapp")["ui"]
        self.assertNotIn("icon", ui)
        self.assertNotIn("color", ui)
        # Other ui keys survive the clear — this edits identity, not the block.
        self.assertEqual(ui["embed"], "iframe")

    def test_preserves_the_rest_of_the_manifest(self):
        hub_api.set_connector_appearance("myapp", {"icon": "chart"})
        m = _read(self.user, "myapp")
        self.assertEqual(m["id"], "myapp")
        self.assertEqual(m["label"], "My App")
        self.assertEqual(m["ui"]["url"], "http://127.0.0.1:9")

    def test_rejects_bad_icon_and_color(self):
        for patch in ({"icon": "../../etc/passwd"}, {"icon": "Cloud; rm -rf /"},
                      {"color": "red"}, {"color": "javascript:alert(1)"},
                      {"color": "var(--app-accent-9)"}):
            r = hub_api.set_connector_appearance("myapp", patch)
            self.assertEqual(r.status_code, 400, patch)
            self.assertFalse(_body(r)["ok"])
        # Nothing was written by any rejected call.
        self.assertNotIn("icon", _read(self.user, "myapp")["ui"])
        self.assertNotIn("color", _read(self.user, "myapp")["ui"])

    def test_empty_patch_rejected(self):
        r = hub_api.set_connector_appearance("myapp", {})
        self.assertEqual(r.status_code, 400)

    def test_unknown_connector_404(self):
        r = hub_api.set_connector_appearance("nosuch", {"icon": "db"})
        self.assertEqual(r.status_code, 404)

    def test_connector_without_ui_block_is_rejected(self):
        # A tool-only connector has no rail tile; adding ui.icon would silently
        # promote it into one.
        _write(self.user, "toolonly", "id: toolonly\nlabel: Tool Only\n")
        connectors.load(force=True)
        r = hub_api.set_connector_appearance("toolonly", {"icon": "db"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("no app tile", _body(r)["error"])
        self.assertNotIn("ui", _read(self.user, "toolonly"))

    def test_builtin_connector_is_read_only(self):
        _write(self.builtin, "shipped", """\
            id: shipped
            label: Shipped
            ui: { embed: iframe, url: "http://127.0.0.1:9" }
        """)
        connectors.load(force=True)
        r = hub_api.set_connector_appearance("shipped", {"icon": "db"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("built-in", _body(r)["error"])


if __name__ == "__main__":
    unittest.main()
