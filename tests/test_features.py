"""The optional-feature registry contract (ava_bridge/features.py).

Every user-facing capability gates its path through features.preflight and
gets REGULAR error codes — ``<key>_off`` / ``<key>_down`` — which the chat UI
turns into guided fix-it links purely from the code pattern. These tests pin
that contract so a new capability only ever needs a registry entry + a
preflight call.
"""
import unittest
from unittest import mock

from ava_bridge import features, settings


def _flag(key: str, value: bool):
    """Patch settings.get_bool so features.<key> reads as `value`."""
    real = settings.get_bool
    def fake(k, default=False, **kw):
        if k == f"features.{key}":
            return value
        return real(k, default, **kw)
    return mock.patch.object(settings, "get_bool", side_effect=fake)


class PreflightContractTests(unittest.TestCase):
    def test_off_yields_key_off_with_selfcontained_message(self):
        for key in features.REGISTRY:
            with _flag(key, False):
                code, msg = features.preflight(key)
            self.assertEqual(code, f"{key}_off")
            # The message must stand alone: agent tools relay it verbatim.
            self.assertIn("Optional features", msg)
            self.assertIn(f"features.{key}", msg)

    def test_off_never_probes(self):
        probe = mock.Mock()
        with _flag("web_search", False):
            code, _ = features.preflight("web_search", probe=probe)
        self.assertEqual(code, "web_search_off")
        probe.assert_not_called()

    def test_on_with_failing_probe_yields_key_down(self):
        with _flag("web_search", True):
            self.assertEqual(
                features.preflight("web_search", probe=lambda: "no answer"),
                ("web_search_down", "no answer"))

    def test_on_with_healthy_probe_passes(self):
        with _flag("web_search", True):
            self.assertIsNone(features.preflight("web_search", probe=lambda: None))
            self.assertIsNone(features.preflight("web_search"))  # probeless gate

    def test_unregistered_key_defaults_on(self):
        # A manifest `feature:` name nobody registered has no UI switch, so it
        # must never read as "off by choice".
        self.assertTrue(features.enabled("definitely_not_registered"))

    def test_snapshot_is_panel_ready(self):
        snap = features.snapshot()
        panel_keys = [k for k, s in features.REGISTRY.items()
                      if s.get("panel", True)]
        self.assertEqual([s["key"] for s in snap], panel_keys)
        # Anything that READS the owner's conversations must be switchable from
        # the panel the UI sends them to. `learning` and `memory` used to carry
        # panel: False, so LearningView's "Enable it in Setup → System" pointed
        # at a checkbox that was filtered out before it ever rendered.
        keys = [s["key"] for s in snap]
        for k in ("learning", "memory", "learning_cloud_fallback"):
            self.assertIn(k, keys, f"{k} reads user conversations and must be "
                                   "togglable from Setup → System")
        for s in snap:
            self.assertEqual(set(s), {"key", "label", "sub", "enabled"})
            self.assertIsInstance(s["enabled"], bool)


if __name__ == "__main__":
    unittest.main()
