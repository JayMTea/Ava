"""Setup → Agent must not shell out four times to paint one panel.

`GET /api/hub/agent/status` calls NemoClawRuntime.status(), which made four
blocking subprocess calls in series: `nemoclaw list --json` from sandbox_info()
(15s), again from available() (10s), a THIRD time uncached (30s), and
`nemoclaw <sandbox> doctor --json` uncached (30s). Every one of those is bounded
only by its timeout, and with the sandbox container down — the exact state that
makes an owner open this panel — nothing answers quickly. The panel sat on
"Loading agent status…" for the better part of a minute while DriftBoard, fed by
a different endpoint, had already painted beneath it.

The fix is a shared cache for the list probe and a cache for doctor, both opt-in
per call site. The opt-in is the load-bearing part and the first test below is
the one that matters: provision() gates on a sandbox the owner may have created
seconds ago with `nemoclaw onboard`, so if it ever reads a stale "not found" it
aborts a provision that should have run.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ava_bridge import config
from ava_bridge.runtime.nemoclaw import NemoClawRuntime

LIST_JSON = json.dumps({
    "schemaVersion": 1,
    "sandboxes": [{"name": "my-assistant", "model": "nvidia/Nemotron",
                   "provider": "compatible-endpoint", "connected": False}],
})


def _rt(calls: list) -> NemoClawRuntime:
    """A runtime whose every subprocess call is recorded instead of run."""
    rt = NemoClawRuntime()

    def fake_run(argv, timeout=120):
        calls.append(argv[1:])          # drop the cli path; keep the verb
        if "doctor" in argv:
            return 0, json.dumps({"ok": True})
        return 0, LIST_JSON

    rt._run = fake_run                   # type: ignore[method-assign]
    return rt


class ProvisionGateStaysFresh(unittest.TestCase):
    """The half of this that must never regress."""

    def test_sandbox_exists_defaults_to_shelling_out(self):
        calls: list = []
        rt = _rt(calls)
        with mock.patch("ava_bridge.runtime.nemoclaw.os.path.exists", return_value=True), \
             mock.patch.object(config, "OC_SANDBOX", "my-assistant"):
            self.assertTrue(rt._sandbox_exists())
            self.assertTrue(rt._sandbox_exists())
        self.assertEqual(len(calls), 2, (
            "_sandbox_exists() served a cached answer by default. provision() "
            "gates on it right after `nemoclaw onboard`, so a stale 'not found' "
            "aborts a provision that should have run — caching must stay opt-in.")
        )

    def test_the_provision_gate_passes_no_max_age(self):
        """Static read of the call site, so the guard survives a refactor of the
        surrounding provisioning code."""
        import inspect
        src = inspect.getsource(NemoClawRuntime.provision)
        assert "self._sandbox_exists()" in src, (
            "provision()'s sandbox gate no longer calls _sandbox_exists() with no "
            "arguments — if it now passes max_age it can read a stale answer.")


class StatusIsCheap(unittest.TestCase):
    def _status(self, rt) -> dict:
        with mock.patch("ava_bridge.runtime.nemoclaw.os.path.exists", return_value=True), \
             mock.patch.object(config, "OC_SANDBOX", "my-assistant"), \
             mock.patch.object(config, "AGENT_ENABLED", True):
            return rt.status()

    def test_one_status_call_makes_two_subprocess_calls_not_four(self):
        calls: list = []
        rt = _rt(calls)
        self._status(rt)
        verbs = [c[0] if c else "" for c in calls]
        self.assertEqual(len(calls), 2, (
            f"status() made {len(calls)} subprocess calls ({verbs}); expected 2 — "
            "one `list --json` shared by sandbox_info/available/sandbox_exists, "
            "and one `doctor`.")
        )

    def test_a_second_poll_reprobes_nothing(self):
        """The panel polls. A second paint inside the cache window must be free —
        this is what turns a ~60s wait into an instant one."""
        calls: list = []
        rt = _rt(calls)
        self._status(rt)
        before = len(calls)
        self._status(rt)
        self.assertEqual(len(calls), before, (
            "a second status() within the cache window shelled out again")
        )

    def test_a_failing_doctor_is_cached_too(self):
        """A dead sandbox is the case that was slow. If only successes cached, the
        broken state — the one an owner is looking at — would still re-probe."""
        calls: list = []
        rt = NemoClawRuntime()

        def fail(argv, timeout=120):
            calls.append(argv[1:])
            return (1, "timed out") if "doctor" in argv else (0, LIST_JSON)

        rt._run = fail                   # type: ignore[method-assign]
        self._status(rt)
        n = len(calls)
        self._status(rt)
        self.assertEqual(len(calls), n, "a failing doctor probe was not cached")
        self.assertIsNone(self._status(rt)["health"])
