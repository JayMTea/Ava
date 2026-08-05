"""Releasing the bridge's own in-process voice models.

ADR-0005 put in-process singletons explicitly out of scope for v1, because giving
them a lever means touching the heap of the process serving the UI while it is
serving. Three properties make that safe, and each pins a real defect:

  * **A release mid-turn cannot 500.** A voice turn used to read `state.heavy[...]`
    at three separate points across a window spanning an ffmpeg decode (up to 30s)
    and a sidecar round-trip. A release landing inside it produced
    `AttributeError: 'NoneType' has no attribute 'transcribe'` — unhandled. Binding
    once at the top turns that into "this turn uses what it started with".
  * **The gate clears as a pair, observably.** `clear_voice_gate` was two bare
    assignments; a reader scheduled between them saw the half-cleared state its own
    docstring says it exists to prevent.
  * **Both ECAPA models go.** The enrolment path keeps its own verifier singleton,
    separate from `state.heavy["verifier"]`. Releasing one and reporting the whole
    would leave ~80 MB behind and lie about it.

Run: .venv/bin/python -m pytest tests/test_voice_release.py -q
"""
import os
import tempfile
import threading
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-voicerel-test-")

from ava_bridge import state, voice_enroll
from ava_bridge.alloc import drivers
from ava_bridge.alloc.base import DriverContext, ReleaseMode


class HeavyTestCase(unittest.TestCase):
    def setUp(self):
        self._orig = dict(state.heavy)
        self.addCleanup(lambda: state.heavy.update(self._orig))
        state.heavy.update(whisper=None, verifier=None, voiceprint=None,
                           voice_unavailable=False)
        voice_enroll._verifier = None
        self.addCleanup(setattr, voice_enroll, "_verifier", None)


class ReleaseTests(HeavyTestCase):
    def test_it_drops_every_voice_model_including_the_enrolment_one(self):
        state.heavy.update(whisper=object(), verifier=object(), voiceprint=object())
        voice_enroll._verifier = object()

        held = state.release_voice_models()

        self.assertEqual(held, {"whisper": True, "verifier": True,
                                "voiceprint": True, "enroll_verifier": True})
        self.assertIsNone(state.heavy["whisper"])
        self.assertIsNone(state.heavy["verifier"])
        self.assertIsNone(state.heavy["voiceprint"])
        self.assertIsNone(voice_enroll._verifier,
                          "the enrolment ECAPA survived — ~80 MB left behind")

    def test_it_never_touches_the_sticky_unavailable_flag(self):
        """That flag means "the deps are not installed" and is one-way.

        Setting it here would disable voice permanently, with no path back: nothing
        ever clears it, and `_ensure_loaded` returns early on it forever.
        """
        state.heavy.update(whisper=object(), voice_unavailable=False)
        state.release_voice_models()
        self.assertFalse(state.heavy["voice_unavailable"])

    def test_it_is_idempotent_and_honest_about_holding_nothing(self):
        held = state.release_voice_models()
        self.assertFalse(any(held.values()))

    def test_the_gate_still_clears_as_a_pair(self):
        state.heavy.update(verifier=object(), voiceprint=object())
        held = state.clear_voice_gate()
        self.assertEqual(held, {"voiceprint": True, "verifier": True})
        self.assertIsNone(state.heavy["verifier"])
        self.assertIsNone(state.heavy["voiceprint"])

    def _blocks_on_heavy_lock(self, fn) -> bool:
        """Does `fn` wait for `heavy_lock`? Run it while this thread holds it.

        Asserted this way, not as "a hammering loop never observed a torn pair": the
        racy form of that test passes against an UNLOCKED implementation on almost
        every run, because the GIL makes the interleave rare. It proves nothing. This
        pins the lock actually being taken, which is what makes tearing impossible.
        """
        done = threading.Event()

        def go():
            fn()
            done.set()

        with state.heavy_lock:
            t = threading.Thread(target=go, daemon=True)
            t.start()
            blocked = not done.wait(0.25)
        t.join(timeout=2)
        return blocked

    def test_clearing_the_gate_takes_the_lock(self):
        state.heavy.update(verifier=object(), voiceprint=object())
        self.assertTrue(
            self._blocks_on_heavy_lock(state.clear_voice_gate),
            "clear_voice_gate mutates without heavy_lock — a reader scheduled "
            "between its two assignments sees verifier set and voiceprint None, "
            "which is spk.cosine(None, emb) -> TypeError on the next voice turn")
        self.assertIsNone(state.heavy["verifier"])

    def test_releasing_takes_the_lock(self):
        state.heavy.update(whisper=object())
        self.assertTrue(self._blocks_on_heavy_lock(state.release_voice_models))

    def test_reading_a_snapshot_takes_the_lock(self):
        self.assertTrue(self._blocks_on_heavy_lock(state.voice_snapshot),
                        "voice_snapshot reads unlocked — it can observe a torn set")


class SnapshotTests(HeavyTestCase):
    def test_a_snapshot_survives_a_release(self):
        """The whole point: a turn holds what it started with.

        The objects also stay ALIVE, which is why a release during a turn frees
        nothing until the turn ends — honest, and what the allocator will measure.
        """
        w, v, p = object(), object(), object()
        state.heavy.update(whisper=w, verifier=v, voiceprint=p)
        snap = state.voice_snapshot()
        state.release_voice_models()
        self.assertEqual(snap, (w, v, p))


class InProcDriverTests(HeavyTestCase):
    def _driver(self):
        ctx = DriverContext(model_id="ava-voice", weight_gb=0.4,
                            config={"target": "voice"})
        return drivers.for_spec("inproc", ctx)

    def test_it_is_registered_and_self_restoring(self):
        drv = self._driver()
        self.assertEqual(drv.name, "inproc")
        # `_ensure_loaded` rebuilds on the next turn, so there is nothing to start.
        self.assertTrue(drv.SELF_RESTORING)
        self.assertEqual(drv.RELEASE_MODES, (ReleaseMode.UNLOAD,))

    def test_residency_is_a_boolean_and_never_a_measured_size(self):
        state.heavy.update(whisper=object())
        with mock.patch.object(drivers.inproc, "_in_bridge", return_value=True):
            res = self._driver().residency()
        self.assertTrue(res.resident)
        # These live inside the PID that also serves the UI; nothing can attribute
        # a few hundred MB within one process, so the weight is a HINT.
        self.assertFalse(res.measured)

    def test_not_installed_is_not_the_same_as_released(self):
        state.heavy.update(voice_unavailable=True)
        with mock.patch.object(drivers.inproc, "_in_bridge", return_value=True):
            res = self._driver().residency()
        self.assertIs(res.resident, False)
        self.assertIn("not installed", res.detail)

    def test_it_declines_from_any_other_process(self):
        """`ava alloc release` is a different PID and cannot touch this heap."""
        state.heavy.update(whisper=object())
        with mock.patch.object(drivers.inproc, "_in_bridge", return_value=False):
            r = self._driver().release(ReleaseMode.UNLOAD)
        self.assertFalse(r.ok)
        # acted=False, so the broker never owes a restore for a no-op.
        self.assertFalse(r.acted)
        self.assertIsNotNone(state.heavy["whisper"])

    def test_a_release_reports_an_unknown_amount_rather_than_the_declared_weight(self):
        state.heavy.update(whisper=object(), verifier=object())
        with mock.patch.object(drivers.inproc, "_in_bridge", return_value=True):
            r = self._driver().release(ReleaseMode.UNLOAD)
        self.assertTrue(r.ok)
        self.assertTrue(r.acted)
        # base.py: freed_gib is MEASURED, never estimated. Reporting weight_gb here
        # would be exactly the guess that contract forbids.
        self.assertIsNone(r.freed_gib)

    def test_releasing_nothing_is_ok_but_did_not_act(self):
        with mock.patch.object(drivers.inproc, "_in_bridge", return_value=True):
            r = self._driver().release(ReleaseMode.UNLOAD)
        self.assertTrue(r.ok)
        self.assertFalse(r.acted)

    def test_an_unknown_target_is_a_config_problem_not_a_crash(self):
        ctx = DriverContext(model_id="x", weight_gb=1.0, config={"target": "nonsense"})
        probs = drivers.for_spec("inproc", ctx).validate()
        self.assertTrue(any("target" in p for p in probs))


if __name__ == "__main__":
    unittest.main()
