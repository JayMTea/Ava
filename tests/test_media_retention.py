"""`data.retention_days` must reach uploaded media.

Media was the only store with no retention at all. Every telemetry store honoured
the setting, so it read as a global disk-use control — while the largest thing Ava
writes per conversation grew forever. The Data page compounded it: media was the
one store registered WITHOUT `managed`, and nothing said why.

Contract mirrors hardware._prune_jsonl: 0 or negative means keep forever.
"""
import os
import tempfile
import time
import unittest
from unittest import mock

from ava_bridge import data_api, settings


class PruneMediaTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ava-media-test-")
        self.up = os.path.join(self.root, "uploads")
        os.makedirs(os.path.join(self.up, "nested"), exist_ok=True)
        p = mock.patch.object(settings, "upload_dir", lambda: self.up)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, path: str, age_days: float, size: int = 128) -> str:
        with open(path, "wb") as f:
            f.write(b"x" * size)
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
        return path

    def test_zero_days_keeps_everything(self):
        old = self._write(os.path.join(self.up, "old.png"), 400)
        res = data_api.prune_media(retention_days=0)
        self.assertEqual(res["removed"], 0)
        self.assertTrue(os.path.exists(old), "0 must mean keep forever")

    def test_negative_days_keeps_everything(self):
        old = self._write(os.path.join(self.up, "old.png"), 400)
        data_api.prune_media(retention_days=-1)
        self.assertTrue(os.path.exists(old))

    def test_removes_only_files_past_the_window(self):
        old = self._write(os.path.join(self.up, "old.png"), 100)
        deep = self._write(os.path.join(self.up, "nested", "deep.pdf"), 100)
        fresh = self._write(os.path.join(self.up, "fresh.png"), 1)

        res = data_api.prune_media(retention_days=30)

        self.assertEqual(res["removed"], 2, res)
        self.assertEqual(res["bytes"], 128 * 2)
        for gone in (old, deep):
            self.assertFalse(os.path.exists(gone), gone)
        self.assertTrue(os.path.exists(fresh), "inside the window must survive")

    def test_dry_run_reports_without_deleting(self):
        old = self._write(os.path.join(self.up, "old.png"), 100)
        res = data_api.prune_media(retention_days=30, dry_run=True)
        self.assertEqual(res["removed"], 1)
        self.assertTrue(res["dry_run"])
        self.assertTrue(os.path.exists(old), "a dry run must not delete")

    def test_missing_directories_are_not_an_error(self):
        with mock.patch.object(settings, "upload_dir", lambda: "/nonexistent/ava/x"):
            res = data_api.prune_media(retention_days=30)
        self.assertEqual(res["removed"], 0)

    def test_reads_the_configured_default_when_not_given(self):
        self._write(os.path.join(self.up, "old.png"), 100)
        with mock.patch.object(settings, "data_retention_days", lambda: 30):
            res = data_api.prune_media()
        self.assertEqual(res["days"], 30)
        self.assertEqual(res["removed"], 1)


class StoresAdvertiseRetention(unittest.TestCase):
    def test_the_media_store_is_marked_managed(self):
        """The Data page's `managed` flag means "retention applies here". Media
        was the only store without it, which was accurate before prune_media()
        existed and would be misleading now."""
        stores = {s["id"]: s for s in data_api.stores()["stores"]}
        self.assertIn("uploads", stores)
        self.assertTrue(stores["uploads"]["managed"], "uploads should be managed")


if __name__ == "__main__":
    unittest.main()
