"""A broken ava.yaml must be loud, and must never be overwritten.

Three failures compounded into one data-loss path:

  * `_load_config` swallowed every parse exception and returned `{}` — so a
    broken config was indistinguishable from no config, and every `get()`
    silently returned its default.
  * `save_patch` re-read from disk, repeated that swallow into `current = {}`,
    deep-merged the patch into nothing, and wrote unconditionally.
  * The write was `write_text`, non-atomic, over the only copy of the file.

Measured before the fix: a 13-line ava.yaml with ONE bad indent, plus one Setup
toggle, became a 2-line file. brand, owner and the entire `alloc` block were
gone — on a box where `alloc.lease.evict: true` is load-bearing, and where the
silent `{}` had already reverted the allocator to advisory without saying so.

The refusal is the important half. Reporting the error only tells you what
happened; refusing the write is what means it did not happen yet.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-seterr-test-"))

import yaml

from ava_bridge import settings

BROKEN = """\
brand:
  name: Ava
owner:
  name: Jay
alloc:
  lease:
    enforce: true
    evict: true
   cooldown_s: 90
"""

GOOD = "brand:\n  name: Ava\nfeatures:\n  voice: true\n"


class _Tmp(unittest.TestCase):
    """Each test gets its own AVA_HOME and restores the module globals."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ava-cfg-")
        self.path = os.path.join(self.dir, "ava.yaml")
        self._patch = mock.patch.object(settings, "CONFIG_PATH",
                                        settings.Path(self.path))
        self._patch.start()
        self._cfg, self._err = settings._CFG, settings._CFG_ERROR
        self.addCleanup(self._restore)

    def _restore(self):
        self._patch.stop()
        settings._CFG, settings._CFG_ERROR = self._cfg, self._err

    def _write(self, body):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(body)

    def _reload(self):
        settings._CFG, settings._CFG_ERROR = settings._load_config()


class LoadErrorTests(_Tmp):
    def test_a_broken_config_reports_an_error_with_a_line_number(self):
        self._write(BROKEN)
        cfg, err = settings._load_config()
        self.assertEqual(cfg, {})
        self.assertTrue(err)
        self.assertIn("line 9", err)          # where the bad indent actually is

    def test_a_good_config_reports_no_error(self):
        self._write(GOOD)
        cfg, err = settings._load_config()
        self.assertEqual(err, "")
        self.assertEqual(cfg["brand"]["name"], "Ava")

    def test_an_absent_config_is_not_an_error(self):
        cfg, err = settings._load_config()
        self.assertEqual((cfg, err), ({}, ""))

    def test_a_non_mapping_top_level_is_an_error(self):
        self._write("- just\n- a list\n")
        _cfg, err = settings._load_config()
        self.assertIn("mapping", err)

    def test_load_error_is_exposed(self):
        self._write(BROKEN)
        self._reload()
        self.assertTrue(settings.load_error())


class RefusalTests(_Tmp):
    def test_save_patch_refuses_and_leaves_the_file_byte_identical(self):
        self._write(BROKEN)
        self._reload()
        before = open(self.path, "rb").read()
        with self.assertRaises(settings.ConfigParseError):
            settings.save_patch({"features": {"voice": False}})
        self.assertEqual(open(self.path, "rb").read(), before)

    def test_save_config_refuses_too(self):
        """save_config's callers build their argument from current_config(),
        which on a broken file is {} — so writing it would delete everything."""
        self._write(BROKEN)
        self._reload()
        before = open(self.path, "rb").read()
        with self.assertRaises(settings.ConfigParseError):
            settings.save_config({"features": {"voice": False}})
        self.assertEqual(open(self.path, "rb").read(), before)

    def test_the_refusal_names_the_file_and_the_problem(self):
        self._write(BROKEN)
        self._reload()
        try:
            settings.save_patch({"a": 1})
        except settings.ConfigParseError as e:
            self.assertIn("ava.yaml", str(e))
            self.assertIn("Refusing to write", str(e))
        else:
            self.fail("the write was not refused")

    def test_a_good_config_still_saves(self):
        self._write(GOOD)
        self._reload()
        merged = settings.save_patch({"features": {"web_search": True}})
        self.assertTrue(merged["features"]["web_search"])
        self.assertTrue(merged["features"]["voice"])       # merge, not replace
        on_disk = yaml.safe_load(open(self.path, encoding="utf-8"))
        self.assertEqual(on_disk["brand"]["name"], "Ava")


class AtomicWriteTests(_Tmp):
    def test_a_backup_of_the_previous_file_is_kept(self):
        self._write(GOOD)
        self._reload()
        settings.save_patch({"features": {"web_search": True}})
        bak = self.path + ".bak"
        self.assertTrue(os.path.isfile(bak))
        self.assertNotIn("web_search", open(bak, encoding="utf-8").read())

    def test_the_config_is_written_0600(self):
        self._write(GOOD)
        self._reload()
        settings.save_patch({"owner": {"name": "Jay"}})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_failed_write_leaves_no_temp_file_and_no_damage(self):
        self._write(GOOD)
        self._reload()
        before = open(self.path, "rb").read()
        with mock.patch.object(settings.os, "replace",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                settings.save_patch({"features": {"web_search": True}})
        self.assertEqual(open(self.path, "rb").read(), before)
        strays = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertFalse(strays, f"left a temp file behind: {strays}")


class GetFloatTests(unittest.TestCase):
    def test_garbage_falls_back_to_the_default(self):
        """ava_bridge/config.py did a bare float() on voice.threshold at import,
        so a non-numeric value there stopped the bridge booting with a bare
        ValueError and no mention of the setting responsible."""
        with mock.patch.dict(os.environ, {"AVA_TEST_FLOAT": "not-a-number"}):
            self.assertEqual(
                settings.get_float("nope.nope", 0.4, env="AVA_TEST_FLOAT"), 0.4)

    def test_a_real_value_is_used(self):
        with mock.patch.dict(os.environ, {"AVA_TEST_FLOAT": "0.75"}):
            self.assertEqual(
                settings.get_float("nope.nope", 0.4, env="AVA_TEST_FLOAT"), 0.75)

    def test_the_bridge_still_imports_with_a_garbage_threshold(self):
        from ava_bridge import config
        self.assertIsInstance(config.PHONE_THRESHOLD, float)


if __name__ == "__main__":
    unittest.main()
