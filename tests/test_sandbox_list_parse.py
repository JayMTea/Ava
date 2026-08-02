"""Ava's brain must never read empty while the agent is plainly answering.

`nemoclaw list --json` is the single source for two facts: whether the sandbox
exists (availability, on the turn path) and which model it thinks with (the
"Ava's brain" panel). Two hand-rolled parsers used to read that one payload —
sandbox_info() scanned for the first `{`, _sandbox_exists() for the first `[` —
and they disagreed with each other and with reality:

  * a top-level array parsed to nothing, so the brain panel went blank while the
    agent answered normally. Nothing in the UI can explain that.
  * `{"warnings": [], "sandboxes": [...]}` made the `[` scanner parse the empty
    warnings list, and the substring fallback underneath it then answered TRUE
    for "error: sandbox 'ava' not found" and for a box named 'ava-old'. A false
    positive there is the expensive direction: available() selects the runtime
    and every turn burns the full ~120s tool timeout before failing.

So the parse is shared, total, and pinned here shape by shape. The failure being
guarded is silent in both directions, which is exactly why it needs a table test
rather than one happy path.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ava_bridge import config
from ava_bridge.runtime.nemoclaw import (
    NemoClawRuntime, _first, _sandbox_records,
)

SBX = "ava"
REC = {"name": SBX, "model": "nvidia/Nemotron",
       "provider": "compatible-endpoint", "connected": True}

# Every shape the payload plausibly arrives in. `_run` merges stderr into stdout,
# so banner text ahead of the JSON is normal, not exotic.
SHAPES = {
    "object with sandboxes[]": json.dumps({"schemaVersion": 1, "sandboxes": [REC]}),
    "top-level array": json.dumps([REC]),
    "a list-valued key before sandboxes": json.dumps({"warnings": [], "sandboxes": [REC]}),
    "banner line, then an object": "warning: gateway stale\n" + json.dumps({"sandboxes": [REC]}),
    "banner line, then an array": "warning: gateway stale\n" + json.dumps([REC]),
    "nested under a parent key": json.dumps({"data": {"sandboxes": [REC]}}),
    "ours is not the first record": json.dumps(
        {"sandboxes": [{"name": "other", "model": "x"}, REC]}),
}


class EveryShapeYieldsTheSandbox(unittest.TestCase):
    def test_the_model_is_found_in_every_shape(self):
        for label, payload in SHAPES.items():
            with self.subTest(shape=label):
                rec = next((r for r in _sandbox_records(payload)
                            if r.get("name") == SBX), None)
                self.assertIsNotNone(rec, f"{label}: no sandbox record parsed")
                self.assertEqual(_first(rec, "model", "modelId"), "nvidia/Nemotron",
                                 f"{label}: the brain would read empty")

    def test_a_renamed_model_field_is_still_found(self):
        """Defensive, not observed: if the CLI ever renames the key, a blank brain
        is the worst possible way to find out."""
        rec = _sandbox_records(json.dumps(
            {"sandboxes": [{"name": SBX, "modelId": "nvidia/Nemotron"}]}))[0]
        self.assertEqual(_first(rec, "model", "modelId", "model_id"), "nvidia/Nemotron")


class NothingIsInventedFromNoise(unittest.TestCase):
    """The other direction. Each of these must yield NO match for `ava`."""

    NOISE = {
        "an error naming the sandbox": "error: sandbox 'ava' not found\n",
        "a different sandbox whose name contains ours":
            json.dumps({"warnings": [], "sandboxes": [{"name": "ava-old"}]}),
        "not json at all": "totally not json",
        "empty output": "",
    }

    def test_no_false_positive(self):
        for label, payload in self.NOISE.items():
            with self.subTest(shape=label):
                self.assertFalse(
                    any(r.get("name") == SBX for r in _sandbox_records(payload)),
                    f"{label}: reported the sandbox as existing. available() would "
                    "then select this runtime and every turn would burn the tool "
                    "timeout.")

    def test_the_runtime_method_does_not_fall_back_to_a_substring(self):
        """Driven through _sandbox_exists(), not the parser — the discarded
        `self.sandbox in out` fallback lived HERE, so testing the parser alone
        would not notice it coming back."""
        for label, payload in self.NOISE.items():
            with self.subTest(shape=label):
                rt = NemoClawRuntime()
                rt._run = lambda argv, timeout=15, _o=payload: (0, _o)  # type: ignore[method-assign]
                with mock.patch("ava_bridge.runtime.nemoclaw.os.path.exists",
                                return_value=True), \
                     mock.patch.object(config, "OC_SANDBOX", SBX):
                    self.assertFalse(rt._sandbox_exists(), (
                        f"{label}: _sandbox_exists() said yes. If this is the "
                        "substring fallback returning, an error message that "
                        "merely NAMES the sandbox now counts as the sandbox "
                        "existing."))


class TheRuntimeAgrees(unittest.TestCase):
    """End to end through the real methods — the two readers must never disagree
    about the same payload, which is the bug that started this."""

    def _rt(self, out: str) -> NemoClawRuntime:
        rt = NemoClawRuntime()
        rt._run = lambda argv, timeout=15: (0, out)  # type: ignore[method-assign]
        return rt

    def test_exists_and_info_agree_on_every_shape(self):
        for label, payload in SHAPES.items():
            with self.subTest(shape=label):
                rt = self._rt(payload)
                with mock.patch("ava_bridge.runtime.nemoclaw.os.path.exists",
                                return_value=True), \
                     mock.patch.object(config, "OC_SANDBOX", SBX):
                    exists = rt._sandbox_exists()
                    info = rt.sandbox_info()
                self.assertTrue(exists, f"{label}: sandbox reported missing")
                self.assertIsNotNone(info, f"{label}: brain reads empty")
                self.assertEqual(info["model"], "nvidia/Nemotron")
                self.assertTrue(info["connected"])

    def test_a_live_sandbox_is_not_reported_as_a_dead_one(self):
        """live() reads `connected` off the same record — the note in the Agent
        panel that says the container is not running comes from here."""
        rt = self._rt(json.dumps([REC]))          # the shape that parsed to nothing
        with mock.patch("ava_bridge.runtime.nemoclaw.os.path.exists", return_value=True), \
             mock.patch.object(config, "OC_SANDBOX", SBX):
            self.assertTrue(rt.live()["live"])
