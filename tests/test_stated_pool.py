"""An owner may STATE a memory pool Ava cannot measure — as advice, never as fact.

Ava withholds a model-tier recommendation when it cannot see the machine the
models actually run on. The case that forced this: an engine on the HOST while
Ava runs in a container (tests/test_host_engine_discovery.py). Withholding is
correct — a tier sized from Ava's own container describes the wrong box — and it
left an owner who KNOWS their machine with no way to say so.

The whole design turns on one boundary, and these tests exist mostly to defend it:

    fit_memory()  is what alloc/capacity.py calls "the one free-memory oracle",
                  and the governor can RELEASE a running model on its word.
                  MEASURED ONLY. A typed number must never reach it: state
                  200 GB and Ava plans against memory that does not exist,
                  over-commits, and the kernel takes real work.

    fit_pool()    recommends, and has exactly one consumer (the setup route).
                  A wrong number here costs a bad suggestion. That is the only
                  cost this override is permitted to have.

Run: .venv/bin/python -m pytest tests/test_stated_pool.py -q
"""
import os
import unittest
from unittest import mock

from ava_bridge import hwinfo, setup_wizard


def _reset():
    hwinfo.reset_cache()


class Precedence(unittest.TestCase):
    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def test_the_env_var_is_read(self):
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": "32"}):
            self.assertEqual(hwinfo.stated_fit_gb(), 32.0)

    def test_config_is_read_when_the_env_is_absent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_FIT_MEM_GB", None)
            with mock.patch("ava_bridge.settings.get", lambda k, d=None: 24 if k == "hardware.fit_memory_gb" else d):
                self.assertEqual(hwinfo.stated_fit_gb(), 24.0)

    def test_the_env_var_beats_config(self):
        """Which is why the Setup form refuses to write when it is set — the
        process would go on ignoring ava.yaml."""
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": "8"}), \
             mock.patch("ava_bridge.settings.get", lambda k, d=None: 64):
            self.assertEqual(hwinfo.stated_fit_gb(), 8.0)

    def test_nothing_stated_is_none(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_FIT_MEM_GB", None)
            with mock.patch("ava_bridge.settings.get", lambda k, d=None: None):
                self.assertIsNone(hwinfo.stated_fit_gb())

    def test_nonsense_is_refused_not_clamped(self):
        """A silently corrected value is one the owner cannot see is wrong, and
        this number's entire job is to be believed."""
        for bad in ("0", "-5", "abc", "99999", ""):
            with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": bad}):
                self.assertIsNone(hwinfo.stated_fit_gb(), bad)

    def test_a_broken_config_never_breaks_the_probe(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_FIT_MEM_GB", None)
            with mock.patch("ava_bridge.settings.get",
                            side_effect=RuntimeError("no config")):
                self.assertIsNone(hwinfo.stated_fit_gb())


class TheBoundary(unittest.TestCase):
    """THE test. Advice may be stated; actuation may not."""

    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def test_the_allocation_oracle_stays_measured(self):
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": "999"}):
            _reset()
            measured = hwinfo.fit_memory()
            _reset()
            advised = hwinfo.fit_pool()
        self.assertEqual(advised.total_gb, 999.0)
        self.assertNotEqual(
            measured.total_gb, 999.0,
            "a typed number reached fit_memory() — alloc can evict on that")
        self.assertNotEqual(measured.source, "stated")

    def test_capacity_reads_the_measured_oracle(self):
        """Pinned against the module that actuates, not just the function name:
        if capacity.py ever switches to fit_pool() this must fail loudly."""
        from ava_bridge.alloc import capacity
        import inspect
        src = inspect.getsource(capacity)
        self.assertIn("fit_memory()", src)
        self.assertNotIn("fit_pool(", src,
                         "the allocation governor must not read the advice pool")


class WhatItReplaces(unittest.TestCase):
    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def test_the_measured_value_is_kept_alongside(self):
        _reset()
        real = hwinfo.fit_pool().measured_gb
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": "16"}):
            _reset()
            p = hwinfo.fit_pool()
        self.assertTrue(p.stated)
        self.assertEqual(p.total_gb, 16.0)
        self.assertEqual(p.measured_gb, real, "what it replaced must stay visible")
        self.assertEqual(p.source, "stated")

    def test_only_the_size_is_stated_not_the_kind(self):
        """Stating "32 GB" says how much, not what — and certainly not that a
        cgroup ceiling stopped existing."""
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": "16"}), \
             mock.patch.object(hwinfo, "_pool_cap", lambda t: (True, "cgroup")):
            _reset()
            p = hwinfo.fit_pool()
        self.assertTrue(p.capped)
        self.assertEqual(p.cap_kind, "cgroup")

    def test_free_is_unknown_rather_than_invented(self):
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": "16"}):
            _reset()
            self.assertIsNone(hwinfo.fit_pool().free_gb)


class TheRouteTellsTheUi(unittest.TestCase):
    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def test_a_stated_pool_drives_the_tier(self):
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": "45"}):
            _reset()
            out = setup_wizard.api_hardware()
        self.assertTrue(out["stated"])
        self.assertEqual(out["fit_gb"], 45.0)
        self.assertEqual(out["tier"], "large")
        self.assertEqual(out["stated_source"], "env")

    def test_overstating_is_flagged_and_understating_is_not(self):
        """Understating is merely cautious. Overstating raises the tier past what
        the box can hold, and the model is killed on load rather than refused."""
        _reset()
        real = hwinfo.fit_pool().measured_gb or 100.0
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": str(real * 4)}):
            _reset()
            self.assertTrue(setup_wizard.api_hardware()["overstated"])
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": str(max(1.0, real / 4))}):
            _reset()
            self.assertFalse(setup_wizard.api_hardware()["overstated"])

    def test_nothing_stated_reports_nothing_stated(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_FIT_MEM_GB", None)
            with mock.patch("ava_bridge.settings.get", lambda k, d=None: d):
                _reset()
                out = setup_wizard.api_hardware()
        self.assertFalse(out["stated"])
        self.assertEqual(out["stated_source"], "")
        self.assertFalse(out["overstated"])


class ChangingItLater(unittest.TestCase):
    """A setting an owner is expected to tune must be tunable, from one place,
    without a reinstall — and must not demand a restart it does not need."""

    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def _post(self, body):
        import asyncio

        class _Req:
            async def json(self):
                return body

        from ava_bridge.hub import hardware as panel
        return asyncio.run(panel.pool_set(_Req()))

    def test_setting_a_value_writes_config_and_needs_no_restart(self):
        seen = {}
        with mock.patch("ava_bridge.settings.save_patch", lambda p: seen.update(p)), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_FIT_MEM_GB", None)
            out = self._post({"gb": 24})
        self.assertEqual(seen, {"hardware": {"fit_memory_gb": 24.0}})
        self.assertTrue(out["ok"])
        self.assertFalse(out["restart_required"],
                         "save_patch updates the live config; a restart banner "
                         "here would demand something that is not needed")

    def test_clearing_it_restores_the_measured_reading(self):
        seen = {}
        with mock.patch("ava_bridge.settings.save_patch", lambda p: seen.update(p)), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_FIT_MEM_GB", None)
            for blank in (None, "", 0):
                seen.clear()
                self._post({"gb": blank})
                self.assertEqual(seen, {"hardware": {"fit_memory_gb": None}}, repr(blank))

    def test_the_save_drops_the_cache_so_the_change_is_visible_now(self):
        """The pool is TTL-cached. Without this the owner saves and watches the
        old number for another three seconds, which reads as the save failing."""
        dropped = []
        with mock.patch("ava_bridge.settings.save_patch", lambda p: None), \
             mock.patch.object(hwinfo, "reset_cache", lambda: dropped.append(1)), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_FIT_MEM_GB", None)
            self._post({"gb": 12})
        self.assertEqual(dropped, [1])

    def test_an_env_override_is_refused_rather_than_silently_ignored(self):
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": "8"}):
            out = self._post({"gb": 24})
        self.assertEqual(out.status_code, 409)

    def test_out_of_range_is_refused_with_the_field_named(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_FIT_MEM_GB", None)
            for bad in (0.2, 99999, "nonsense"):
                out = self._post({"gb": bad})
                self.assertEqual(out.status_code, 400, repr(bad))

    def test_the_get_says_whether_this_form_may_change_it(self):
        from ava_bridge.hub import hardware as panel
        with mock.patch.dict(os.environ, {"AVA_FIT_MEM_GB": "8"}):
            self.assertFalse(panel.pool_get()["editable"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVA_FIT_MEM_GB", None)
            with mock.patch("ava_bridge.settings.get", lambda k, d=None: None):
                self.assertTrue(panel.pool_get()["editable"])


if __name__ == "__main__":
    unittest.main()
