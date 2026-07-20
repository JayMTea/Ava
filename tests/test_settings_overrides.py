"""Env-override honesty helpers (settings.env_override / explicitly_false).

These back the Hub's "your yaml edit is shadowed by an env var" notes and the
explicit-off gates (e.g. /api/talk honoring features.voice:false) added in the
truth-source audit. Unset must never read as off, and only a set env var may
count as an override.
"""
import os
import unittest
from unittest import mock

from ava_bridge import settings


class EnvOverrideTests(unittest.TestCase):
    def test_set_env_var_reports_its_name(self):
        with mock.patch.dict(os.environ, {"QA_TEST_FLAG": "1"}):
            self.assertEqual(settings.env_override("QA_TEST_FLAG"), "QA_TEST_FLAG")

    def test_unset_and_empty_are_not_overrides(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QA_TEST_FLAG", None)
            self.assertIsNone(settings.env_override("QA_TEST_FLAG"))
        with mock.patch.dict(os.environ, {"QA_TEST_FLAG": ""}):
            self.assertIsNone(settings.env_override("QA_TEST_FLAG"))


class ExplicitlyFalseTests(unittest.TestCase):
    def test_unset_is_not_off(self):
        with mock.patch.object(settings, "get", return_value=None):
            self.assertFalse(settings.explicitly_false("features.voice"))

    def test_yaml_false_is_off(self):
        for v in (False, "false", "0", "no", "off"):
            with mock.patch.object(settings, "get", return_value=v):
                self.assertTrue(settings.explicitly_false("features.voice"),
                                f"value {v!r} should read as explicitly off")

    def test_true_values_are_not_off(self):
        for v in (True, "true", "1", "yes"):
            with mock.patch.object(settings, "get", return_value=v):
                self.assertFalse(settings.explicitly_false("features.voice"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
