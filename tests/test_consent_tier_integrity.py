"""A consent tier the author misspelled must not become a quieter one.

Ava's JIT consent has four tiers (`read` silent, `write` asks once and may be
always-allowed, `destructive`/`physical` ask every time and never can be). Which
one applies is decided by a string in a manifest, so the failure mode that matters
is a string that does not parse.

Two ways that used to go wrong, both silent:

1. `_dynamic_access` required `tier in _TIERS` **and** an fnmatch hit in the same
   condition, so an unreadable tier made the pattern simply not match. A manifest
   saying `"*publish*": destrucive` fell through to the `write` fallback —
   grantable, always-allowable, no error row anywhere. The author asked for the
   strictest gate and got the weakest.

2. `confirm: [action_names]` was declared `bool` in `_BLOCK_TYPES`, so `_validate`
   popped the whole block before `_author_confirm` — which implements the list
   form — ever saw it. This survived because `tests/test_approvals.py` exercises
   the list form through a mocked `connectors.load`, so the validator never ran on
   that path. A loader-level test is the only kind that catches it.

Both now fail CLOSED and are reported as `severity: error`.
"""
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors


def _with(manifest):
    return mock.patch.object(connectors, "load", return_value=[manifest])


class MisspelledDynamicTierTests(unittest.TestCase):
    BAD = {"id": "typo", "mcp": {"url": "http://127.0.0.1:9200/mcp"},
           "dynamic_access": {"*publish*": "destrucive", "*": "write"}}

    def test_a_matched_pattern_with_a_bad_tier_fails_closed(self):
        with _with(self.BAD):
            self.assertEqual(connectors.action_access("typo", "publish_app"),
                             "destructive")

    def test_and_is_therefore_not_grantable(self):
        with _with(self.BAD):
            self.assertFalse(connectors.grantable("typo", "publish_app"))
            self.assertTrue(connectors.needs_confirm("typo", "publish_app"))

    def test_unmatched_tools_are_unaffected(self):
        """Fail-closed applies to the pattern that MATCHED, not to everything."""
        with _with(self.BAD):
            self.assertEqual(connectors.action_access("typo", "list_apps"), "write")

    def test_a_valid_tier_still_resolves_normally(self):
        good = {"id": "ok", "mcp": {"url": "http://127.0.0.1:9200/mcp"},
                "dynamic_access": {"get_*": "read", "*": "write"}}
        with _with(good):
            self.assertEqual(connectors.action_access("ok", "get_thing"), "read")
            self.assertEqual(connectors.action_access("ok", "do_thing"), "write")

    def test_first_match_still_wins(self):
        m = {"id": "order", "mcp": {"url": "http://127.0.0.1:9200/mcp"},
             "dynamic_access": {"get_secret": "destructive", "get_*": "read"}}
        with _with(m):
            self.assertEqual(connectors.action_access("order", "get_secret"),
                             "destructive")
            self.assertEqual(connectors.action_access("order", "get_other"), "read")


class ValidationReportsBadTiersTests(unittest.TestCase):
    def test_a_bad_dynamic_tier_is_an_error_row(self):
        m = {"id": "typo", "dynamic_access": {"*publish*": "destrucive"}}
        errors: list = []
        connectors._validate(m, "t.yaml", errors)
        self.assertEqual(len(errors), 1, errors)
        self.assertEqual(errors[0]["severity"], "error")
        self.assertIn("destrucive", errors[0]["error"])
        self.assertIn("fail", errors[0]["error"].lower())

    def test_a_bad_static_access_is_an_error_row(self):
        m = {"id": "s", "actions": [{"id": "a", "method": "POST", "path": "/x",
                                     "access": "writ"}]}
        errors: list = []
        connectors._validate(m, "t.yaml", errors)
        self.assertTrue(any(e["severity"] == "error" and "writ" in e["error"]
                            for e in errors), errors)

    def test_a_good_manifest_produces_no_rows(self):
        m = {"id": "g", "dynamic_access": {"get_*": "read", "*": "write"},
             "actions": [{"id": "a", "method": "POST", "path": "/x", "access": "write"}]}
        errors: list = []
        connectors._validate(m, "t.yaml", errors)
        self.assertEqual(errors, [])


class ConfirmListSurvivesTheLoaderTests(unittest.TestCase):
    """The loader path, not a mocked registry — that is the whole point."""

    def _load(self, body: str) -> tuple[dict, list]:
        import os
        import tempfile
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "cft"), exist_ok=True)
        with open(os.path.join(d, "cft", "connector.yaml"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(body))
        with mock.patch.object(connectors, "BUILTIN_DIR", d), \
                mock.patch.object(connectors, "USER_DIR", d):
            items = connectors.load(force=True)
            errs = connectors.load_errors()
        return ({x["id"]: x for x in items}.get("cft") or {}), errs

    def tearDown(self):
        connectors.load(force=True)

    def test_the_list_form_is_not_stripped(self):
        m, errs = self._load("""
            id: cft
            label: Confirm list
            mcp: {url: "http://127.0.0.1:9200/mcp"}
            confirm: [publish_app, delete_app]
        """)
        self.assertEqual(m.get("confirm"), ["publish_app", "delete_app"],
                         f"confirm was dropped by the loader; errors={errs}")
        self.assertEqual([e for e in errs if e.get("id") == "cft"], [])

    def test_and_the_gate_it_asks_for_actually_fires(self):
        m, _ = self._load("""
            id: cft
            label: Confirm list
            mcp: {url: "http://127.0.0.1:9200/mcp"}
            confirm: [publish_app]
            dynamic_access: {"*": write}
        """)
        with _with(m):
            self.assertTrue(connectors.needs_confirm("cft", "publish_app"))
            self.assertFalse(connectors.grantable("cft", "publish_app"),
                             "an author-confirmed action must not be always-allowable")
            # A different action is untouched by the list.
            self.assertTrue(connectors.grantable("cft", "list_apps"))

    def test_the_bool_form_still_works(self):
        m, errs = self._load("""
            id: cft
            label: Confirm all
            mcp: {url: "http://127.0.0.1:9200/mcp"}
            confirm: true
        """)
        self.assertIs(m.get("confirm"), True)
        self.assertEqual([e for e in errs if e.get("id") == "cft"], [])

    def test_a_genuinely_wrong_type_is_still_quarantined(self):
        m, errs = self._load("""
            id: cft
            label: Bad confirm
            confirm: "yes please"
        """)
        self.assertNotIn("confirm", m)
        self.assertTrue(any(e.get("id") == "cft" and e["severity"] == "error"
                            for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
