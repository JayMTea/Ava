"""Is what is RUNNING still what is on disk?

Four things in this system are frozen at a moment and then quietly diverge from
the world:

    the imported Python      frozen at process boot
    settings._CFG            frozen at MODULE IMPORT (there is no reload)
    the router's backends    frozen by create_app(), called once
    the engine's inventory   never re-read at all  (that one is serving_truth)

On 2026-08-13 the first was found four days out of date: a router had been
serving a hardcoded default deleted from the tree, so the constant existed
nowhere on disk and `grep` found nothing. Uptime cannot express this — "running
for four days" and "running the WRONG four-day-old code" are the same reading.
Only comparing a boot stamp against the tree can.

The discipline every test here enforces is the same one the residency probes
keep: an unknown stamp degrades to "we cannot tell", NEVER to "stale". A
staleness report that cries wolf gets dismissed, and then the real one is
dismissed with it.

Run: .venv/bin/python -m pytest tests/test_staleness.py -q
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-stale-test-")

from ava_bridge import settings, version


class CodeDriftTests(unittest.TestCase):
    def test_identical_stamps_are_not_stale(self):
        stamp = {"revision": "abc123", "source_mtime": 1000.0}
        with mock.patch.object(version, "BOOT_STAMP", stamp), \
             mock.patch.object(version, "tree_stamp", return_value=dict(stamp)):
            d = version.code_drift()
        self.assertFalse(d["stale"])
        self.assertTrue(d["known"])

    def test_newer_source_on_disk_is_stale(self):
        """The reading that actually moves when someone edits or pulls."""
        with mock.patch.object(version, "BOOT_STAMP",
                               {"revision": "abc123", "source_mtime": 1000.0}), \
             mock.patch.object(version, "tree_stamp",
                               return_value={"revision": "abc123",
                                             "source_mtime": 1600.0}):
            d = version.code_drift()
        self.assertTrue(d["stale"])
        self.assertEqual(d["since_s"], 600.0)

    def test_a_changed_revision_is_stale(self):
        with mock.patch.object(version, "BOOT_STAMP",
                               {"revision": "abc123", "source_mtime": 1000.0}), \
             mock.patch.object(version, "tree_stamp",
                               return_value={"revision": "def456",
                                             "source_mtime": 1000.0}):
            self.assertTrue(version.code_drift()["stale"])

    def test_older_source_on_disk_is_not_stale(self):
        """A checkout moving BACKWARD is a rollback someone did on purpose, and
        the process is still running the newer code. Not this alert's business."""
        with mock.patch.object(version, "BOOT_STAMP",
                               {"revision": "", "source_mtime": 2000.0}), \
             mock.patch.object(version, "tree_stamp",
                               return_value={"revision": "",
                                             "source_mtime": 1000.0}):
            self.assertFalse(version.code_drift()["stale"])

    def test_an_unknown_stamp_is_not_stale(self):
        """No git, no readable mtimes: say so, do not guess."""
        with mock.patch.object(version, "BOOT_STAMP",
                               {"revision": "", "source_mtime": 0.0}), \
             mock.patch.object(version, "tree_stamp",
                               return_value={"revision": "", "source_mtime": 0.0}):
            d = version.code_drift()
        self.assertFalse(d["stale"])
        self.assertFalse(d["known"])

    def test_a_baked_revision_alone_cannot_hide_an_edit(self):
        """The Docker case, and the reason mtime is carried at all.

        An image bakes AVA_REVISION at build time, so after a `git pull` on the
        running box the revision still matches while the code on disk has moved.
        """
        with mock.patch.object(version, "BOOT_STAMP",
                               {"revision": "abc123", "source_mtime": 1000.0}), \
             mock.patch.object(version, "tree_stamp",
                               return_value={"revision": "abc123",
                                             "source_mtime": 9999.0}):
            self.assertTrue(version.code_drift()["stale"])

    def test_the_boot_stamp_cannot_refresh_itself(self):
        """It is a module constant for exactly this reason: a boot stamp that
        re-read the tree would always agree with it, and always say fine."""
        first = version.boot_stamp()
        second = version.boot_stamp()
        self.assertEqual(first, second)
        self.assertIsNot(first, version.BOOT_STAMP)     # a copy, not the original


class ConfigDriftTests(unittest.TestCase):
    def test_an_untouched_config_is_not_changed(self):
        # Both sides pinned. `_CFG_DIGEST` is process-global and every save path
        # re-stamps it, so reading the live one here would make this test pass or
        # fail on whether some other test file happened to save a config first.
        cfg = {"server": {"host": "127.0.0.1"}, "setup": {"completed": True}}
        with mock.patch.object(settings, "_CFG_DIGEST", settings._digest(cfg)), \
             mock.patch.object(settings, "_load_config",
                               return_value=(dict(cfg), "")):
            d = settings.config_drift()
        self.assertFalse(d["changed"])
        self.assertTrue(d["known"])

    def test_an_external_edit_is_detected(self):
        """The gap this closes: the restart banner is raised by a mutation
        response, and a text editor does not send one."""
        with mock.patch.object(settings, "_load_config",
                               return_value=({"server": {"host": "0.0.0.0"},
                                              "brand": {"name": "Edited"}}, "")):
            d = settings.config_drift()
        self.assertTrue(d["changed"])
        self.assertTrue(d["known"])

    def test_key_order_is_not_a_change(self):
        """Otherwise a rewritten-but-equivalent file would demand a restart."""
        with mock.patch.object(settings, "_CFG_DIGEST",
                               settings._digest({"a": 1, "b": {"c": 2, "d": 3}})), \
             mock.patch.object(settings, "_load_config",
                               return_value=({"b": {"d": 3, "c": 2}, "a": 1}, "")):
            self.assertFalse(settings.config_drift()["changed"])

    def test_an_unparseable_config_is_not_reported_as_changed(self):
        """It has its own message (`load_error`). Telling the owner to restart
        to apply something Ava could not read would send them the wrong way."""
        with mock.patch.object(settings, "_load_config",
                               return_value=({}, "expected a mapping")):
            d = settings.config_drift()
        self.assertFalse(d["changed"])
        self.assertFalse(d["known"])

    def test_a_save_through_ava_does_not_look_like_an_external_edit(self):
        """Both save paths re-stamp the digest; without that, every write from
        the UI would immediately raise 'ava.yaml changed on disk' against
        itself."""
        original = settings._CFG_DIGEST
        self.addCleanup(setattr, settings, "_CFG_DIGEST", original)
        new_cfg = {"server": {"host": "127.0.0.1"}, "marker": "saved"}
        settings._CFG_DIGEST = settings._digest(new_cfg)
        with mock.patch.object(settings, "_load_config",
                               return_value=(dict(new_cfg), "")):
            self.assertFalse(settings.config_drift()["changed"])


class SystemPayloadTests(unittest.TestCase):
    def test_staleness_always_has_all_three_keys(self):
        from ava_bridge.hub import system

        s = system._staleness()
        self.assertEqual(set(s), {"code", "config", "router"})

    def test_a_failing_probe_degrades_to_unknown_not_stale(self):
        from ava_bridge.hub import system

        with mock.patch.object(version, "code_drift",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(settings, "config_drift",
                               side_effect=RuntimeError("boom")):
            s = system._staleness()
        self.assertFalse(s["code"]["stale"])
        self.assertFalse(s["code"]["known"])
        self.assertFalse(s["config"]["changed"])
        self.assertFalse(s["config"]["known"])


if __name__ == "__main__":
    unittest.main()
