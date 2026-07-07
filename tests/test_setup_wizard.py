"""settings.save_patch + wizard completion gate."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ava_bridge import settings


class SavePatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Path(self.tmp) / "ava.yaml"
        self._p = mock.patch.object(settings, "CONFIG_PATH", self.cfg)
        self._p.start()
        self._cfg_backup = settings._CFG
        settings._CFG = {}

    def tearDown(self):
        self._p.stop()
        settings._CFG = self._cfg_backup

    def test_deep_merge_writes_and_refreshes(self):
        settings.save_patch({"inference": {"primary": "local",
                                           "backends": {"local": {"engine": "ollama"}}}})
        self.assertEqual(settings.get("inference.primary"), "local")
        # A second patch merges rather than clobbering the sibling key.
        settings.save_patch({"inference": {"reasoning": "off"}})
        self.assertEqual(settings.get("inference.primary"), "local")
        self.assertEqual(settings.get("inference.reasoning"), "off")
        self.assertTrue(self.cfg.is_file())

    def test_setup_completed_flag_persists(self):
        settings.save_patch({"setup": {"completed": True}})
        self.assertTrue(settings.get_bool("setup.completed", False))


if __name__ == "__main__":
    unittest.main()
