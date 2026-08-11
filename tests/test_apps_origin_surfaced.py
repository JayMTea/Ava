"""The same-origin warning has to reach a screen, not just an endpoint.

`ava_bridge/apps_origin.py` documents the hole precisely: with `apps.origin`
unset — the DEFAULT — an embedded connector UI is genuinely same-origin with the
shell, so its JavaScript runs with the owner's session and can call
`/api/hub/*`, including approving Ava's own consent prompts. `warning()` exists
to say so and `/api/apps` has shipped it since the day it was written.

Nothing rendered it. `grep apps_origin frontend/src` returned only comments —
including `AppFrame.tsx`, which asserted "that is the hole apps.origin exists to
close, and Setup says so." Setup did not say so, and the connect form said the
opposite: "served same-origin so it just works."

A fact the backend computes and no surface shows is worth exactly nothing, and
the only way to keep that true is to assert the wiring. These tests pin the
payload AND the consumer, because the payload alone is what shipped for months.
"""
from __future__ import annotations

import pathlib
import re
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class WarningContentTests(unittest.TestCase):

    def test_unset_is_not_ok_and_explains_the_consequence(self):
        from ava_bridge import apps_origin

        with mock.patch.object(apps_origin, "configured", return_value=None):
            w = apps_origin.warning()
        self.assertFalse(w["ok"])
        self.assertIsNone(w["origin"])
        self.assertTrue(w["detail"])

    def test_configured_is_ok_and_names_the_origin(self):
        from ava_bridge import apps_origin

        with mock.patch.object(apps_origin, "configured",
                               return_value="http://apps.ava.local:8096"):
            w = apps_origin.warning()
        self.assertTrue(w["ok"])
        self.assertEqual(w["origin"], "http://apps.ava.local:8096")


class PayloadTests(unittest.TestCase):

    def test_the_connector_management_page_is_told(self):
        """`/api/apps` drives the nav rail. The DECISION — give this app a tile —
        is made on Setup → Connectors, so the fact has to be on that payload too."""
        from ava_bridge.hub import connectors as hub_connectors

        with mock.patch.object(hub_connectors.connectors, "catalog", return_value=[]), \
             mock.patch.object(hub_connectors.connectors, "load_errors", return_value=[]):
            payload = hub_connectors.list_connectors()
        self.assertIn("apps_origin", payload)
        self.assertIn("ok", payload["apps_origin"])


class ConsumerTests(unittest.TestCase):
    """Static scan. The bug was never in the backend."""

    def _read(self, rel: str) -> str:
        return (FRONTEND / rel).read_text(encoding="utf-8")

    def test_a_panel_renders_it_rather_than_only_mentioning_it(self):
        src = self._read("components/hub/panels/ConnectorsPanel.tsx")
        # A comment is what shipped for months. Require a real read of the field.
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("//"))
        self.assertTrue(
            re.search(r"apps_origin", code),
            "no panel reads apps_origin — the warning is computed and thrown away")
        self.assertIn("apps.origin", src,
                      "the banner has to name the setting that fixes it")

    def test_the_connect_form_no_longer_promises_it_just_works(self):
        """The sentence sat directly under the checkbox that creates the frame."""
        src = self._read("components/hub/ConnectApp.tsx")
        self.assertNotIn("same-origin so it just works", src)
        self.assertIn("apps.origin", src,
                      "the sidebar checkbox has to say what ticking it means")

    def test_the_frame_comment_points_at_a_surface_that_exists(self):
        src = self._read("components/AppFrame.tsx")
        if "Setup" in src and "apps.origin" in src:
            self.assertIn("ConnectorsPanel", src,
                          "AppFrame claims Setup says so; name the file that does, "
                          "so the claim can be checked when it moves")


if __name__ == "__main__":
    unittest.main()
