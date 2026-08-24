"""The fake gateway must agree with the REAL one, not with its callers.

This is the guard for the failure mode behind every other bug on this branch: a
fake built from the same assumption as the caller AGREES WITH THE CALLER. Five
invented method names, an invented event topic and half a dozen wrong param
shapes all passed the whole test suite and failed in the browser, because the
fake had been taught the same fiction.

`qa/fakes/gateway-schemas.json` is learned from the live gateway's own
INVALID_REQUEST messages. This file holds the fake to it:

  * the fake's schemas are LOADED from the capture, not written from memory;
  * anything it hand-writes on top must not CONTRADICT the capture;
  * it actually refuses the mistakes that really shipped (anti-vacuous);
  * the capture itself is stamped, substantial, and honest about its gaps.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(ROOT, "qa", "fakes")
sys.path.insert(0, FAKES)

import fake_gateway as fg  # noqa: E402

CAPTURE = json.load(open(os.path.join(FAKES, "gateway-schemas.json"),
                         encoding="utf-8"))
METHODS = CAPTURE["methods"]


class CaptureTests(unittest.TestCase):

    def test_the_capture_is_stamped_with_what_it_came_from(self):
        """A schema set with no provenance is indistinguishable from a guess."""
        cap = CAPTURE["_capture"]
        self.assertTrue(cap.get("gateway_version"),
                        "the capture must name the build it was learned from")
        self.assertEqual(cap.get("protocol"), 4)

    def test_the_capture_is_substantial(self):
        """Anti-vacuous: a truncated capture would make everything below pass
        by checking nothing."""
        learned = [m for m, s in METHODS.items() if s.get("learned")]
        self.assertGreater(len(learned), 30, "the capture looks truncated")

    def test_the_capture_records_what_it_could_not_learn(self):
        """Silence about a gap reads as 'fully captured', which is the claim
        that made a guess look like a fact in the first place."""
        gaps = CAPTURE["_capture"].get("gaps")
        self.assertIsInstance(gaps, list)
        for gap in gaps:
            self.assertGreater(len(gap), 60,
                               f"a gap is recorded without saying what blocks "
                               f"capture: {gap!r}")

    def test_a_partial_capture_is_not_enforced(self):
        """`cron.add` was under-captured (its schedule is an object and the
        probe only sends strings). Enforcing a schema we did not fully learn
        would refuse calls the real gateway accepts — a fake that is WRONGLY
        strict is as misleading as one that is too loose."""
        self.assertFalse(METHODS["cron.add"].get("learned"))
        self.assertNotIn("cron.add", fg._load_captured_schemas())


class FakeSourcingTests(unittest.TestCase):

    def test_the_fake_loads_its_schemas_from_the_capture(self):
        src = open(fg.__file__, encoding="utf-8").read()
        self.assertIn("_load_captured_schemas", src)
        self.assertIn("gateway-schemas.json", src)

    def test_it_actually_loaded_a_useful_number_of_them(self):
        self.assertGreater(len(fg._PARAM_SCHEMAS), 30)

    def test_every_hand_written_schema_agrees_with_the_capture(self):
        """The dangerous case: an override that CONTRADICTS what the gateway
        told us reintroduces exactly the bug the capture exists to prevent."""
        wrong = []
        for method, extra in fg._EXTRA_SCHEMAS.items():
            got = METHODS.get(method)
            if not got or not got.get("learned"):
                continue        # nothing captured to contradict
            for name in extra["required"]:
                if name not in got["allowed"]:
                    wrong.append(f"{method}: requires '{name}', which the "
                                 f"gateway refuses as unexpected")
            for name in got["required"]:
                if name not in extra["required"]:
                    wrong.append(f"{method}: the gateway REQUIRES '{name}' and "
                                 f"the override does not")
        self.assertEqual(wrong, [], "\n  ".join([""] + wrong))

    def test_the_fake_advertises_only_methods_the_gateway_has(self):
        live = {ln.strip() for ln in
                open(os.path.join(FAKES, "gateway-methods.txt"),
                     encoding="utf-8")
                if ln.strip() and not ln.startswith("#")}
        extra = sorted(set(fg.DEFAULT_METHODS) - live)
        self.assertEqual(extra, [],
                         f"the fake offers methods no gateway has: {extra}")


class RefusalTests(unittest.TestCase):
    """The mistakes that REALLY shipped must fail here now.

    Each of these passed the suite once and broke in the browser.
    """

    CASES = [
        ("chat.history", {"sessionId": "x"}, "sessionId, not sessionKey"),
        ("sessions.delete", {"sessionKey": "x"}, "sessionKey, not key"),
        ("terminal.open", {"sessionId": "x"}, "open takes cols/rows"),
        ("terminal.input", {"terminalId": "x", "data": "y"},
         "terminalId is not a field the gateway has"),
        ("chat.abort", {"runId": "x"}, "sessionKey is required"),
        ("sessions.patch", {"key": "k", "title": "new"},
         "there is no title field — rename is not a patch"),
        ("exec.approval.resolve", {"id": "a"}, "decision is required"),
    ]

    def test_each_real_mistake_is_refused(self):
        for method, params, why in self.CASES:
            with self.subTest(method=method, why=why):
                self.assertIsNotNone(
                    fg._invalid_params(method, params),
                    f"the fake accepts {method} with {params} — {why}. This is "
                    "how the bug shipped: the fake agreed with the caller.")

    def test_a_wrong_type_is_refused(self):
        """The capture learns types too; `cols` is an integer."""
        self.assertIsNotNone(
            fg._invalid_params("terminal.open", {"cols": "80", "rows": 24}))

    def test_correct_calls_are_accepted(self):
        """The other half: a fake that refuses everything catches nothing and
        blocks everything."""
        for method, params in (
                ("chat.history", {"sessionKey": "x", "limit": 8}),
                ("terminal.open", {"cols": 80, "rows": 24}),
                ("exec.approval.resolve", {"id": "a", "decision": "deny"}),
                ("chat.abort", {"sessionKey": "s", "runId": "r"})):
            with self.subTest(method=method):
                self.assertIsNone(fg._invalid_params(method, params))

    def test_an_unknown_method_is_not_validated(self):
        """No schema means no opinion — inventing one would refuse calls the
        real gateway accepts."""
        self.assertIsNone(fg._invalid_params("not.a.method", {"anything": 1}))


if __name__ == "__main__":
    unittest.main()
