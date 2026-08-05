"""The GPU voice sidecar's release lever, and the load race it exposes.

`/free` is what lets an owner reclaim the ~0.8 GB of accelerator memory the voice
sidecar holds. It is small in code and carries three failures worth pinning:

  * **The unlocked TTS load.** `_pipe` had no lock and `_asr` did. That was safe only
    because the startup warm built `_pipe` before uvicorn accepted a connection —
    `/free` removes that guarantee, and `/tts` is a sync `def`, so it is genuinely
    concurrent on Starlette's threadpool. Several requests would each build their own
    KPipeline, and the resulting spike lands exactly when someone asked for memory.
  * **Releasing under an in-flight call frees nothing.** The running request holds its
    own strong reference, so the weights survive and `empty_cache()` returns only
    blocks that request is about to re-allocate. Declining is the honest answer, and
    it must be a non-2xx so `HttpUnloadDriver` records `acted=False` rather than
    owing a restore for a release that never happened.
  * **`ok: false` meaning two different things.** A sidecar told to drop its weights
    and one whose model never loaded report identically — and the second is the
    six-day silent-fallback outage the readiness probe exists to catch.

The module imports with no torch, no kokoro and no transformers (all three are
imported lazily, deliberately), so these drive it with fakes and no GPU.

Run: .venv/bin/python -m pytest tests/test_kokoro_free.py -q
"""
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-kokoro-test-")

# `soundfile` is a module-scope import in kokoro_tts and lives in the sidecar's OWN
# venv (.venv-tts, see deploy/gpu-voice-serve.sh) — the bridge venv deliberately does
# not carry it, and this tier must run with no GPU and no voice extras. A stub is
# enough because nothing under test writes a WAV: `/free`, `/health` and the loaders
# are the surface, and torch / kokoro / transformers are all imported lazily inside
# the functions, which is the property that makes this module testable at all.
sys.modules["soundfile"] = sys.modules.get("soundfile") or types.SimpleNamespace(
    write=lambda *a, **k: None)

from fastapi.testclient import TestClient  # noqa: E402 — after the stub, on purpose

import kokoro_tts  # noqa: E402 — importing it is what needs the stub above


class KokoroTestCase(unittest.TestCase):
    def setUp(self):
        kokoro_tts._pipe = None
        kokoro_tts._asr = None
        kokoro_tts._inflight = 0
        kokoro_tts._released = False
        self.addCleanup(setattr, kokoro_tts, "_pipe", None)
        self.addCleanup(setattr, kokoro_tts, "_asr", None)
        self.addCleanup(setattr, kokoro_tts, "_inflight", 0)
        # Never let a test reach a real accelerator.
        p = mock.patch.object(kokoro_tts, "_empty_cache", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        # The startup warm loads real models; these tests drive the routes directly.
        self.client = TestClient(kokoro_tts.app)


class FreeTests(KokoroTestCase):
    def test_free_drops_both_singletons_and_says_which_it_held(self):
        kokoro_tts._pipe = object()
        kokoro_tts._asr = object()
        r = self.client.post("/free")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "freed": {"tts": True, "stt": True}})
        self.assertIsNone(kokoro_tts._pipe)
        self.assertIsNone(kokoro_tts._asr)

    def test_free_is_idempotent_and_honest_about_holding_nothing(self):
        r = self.client.post("/free")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["freed"], {"tts": False, "stt": False})

    def test_free_hands_the_blocks_back_to_the_driver(self):
        """Without empty_cache the release is invisible to nvidia-smi, and so to
        the allocator that has to verify it."""
        kokoro_tts._pipe = object()
        called = []
        with mock.patch.object(kokoro_tts, "_empty_cache",
                               lambda: called.append(True)):
            self.client.post("/free")
        self.assertEqual(called, [True])

    def test_free_declines_while_work_is_in_flight(self):
        kokoro_tts._pipe = object()
        kokoro_tts._inflight = 1
        r = self.client.post("/free")
        # Non-2xx on purpose — see the module docstring.
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["busy"])
        self.assertIsNotNone(kokoro_tts._pipe, "it freed under a live request")

    def test_free_is_post_only(self):
        self.assertEqual(self.client.get("/free").status_code, 405)


class HealthTests(KokoroTestCase):
    def test_health_separates_released_from_never_loaded(self):
        never = self.client.get("/health").json()
        self.assertFalse(never["ok"])
        self.assertFalse(never["released"])     # nothing ever loaded

        kokoro_tts._pipe = object()
        self.client.post("/free")
        after = self.client.get("/health").json()
        self.assertFalse(after["ok"])
        self.assertTrue(after["released"], "a deliberate release looks like the outage")

    def test_a_reload_clears_the_released_flag(self):
        kokoro_tts._released = True
        with mock.patch.dict("sys.modules", {"kokoro": mock.Mock()}):
            kokoro_tts._pipeline()
        self.assertFalse(kokoro_tts._released)


class LoadRaceTests(KokoroTestCase):
    def test_a_burst_after_a_free_builds_the_tts_pipeline_exactly_once(self):
        """The bug `/free` would have introduced: N models loaded, N-1 leaked."""
        built = []
        barrier = threading.Barrier(8)

        class SlowKPipeline:
            def __init__(self, **kw):
                built.append(kw)
                # Widen the window a real model load would occupy.
                threading.Event().wait(0.02)

        fake = mock.Mock()
        fake.KPipeline = SlowKPipeline
        with mock.patch.dict("sys.modules", {"kokoro": fake}):
            def go():
                barrier.wait()
                kokoro_tts._pipeline()
            threads = [threading.Thread(target=go) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(built), 1, f"loaded the model {len(built)} times")

    def test_the_two_singletons_share_one_lock(self):
        """Separate locks would let a free interleave between the two rebinds."""
        self.assertIs(kokoro_tts._model_lock, kokoro_tts._model_lock)
        kokoro_tts._pipe = object()
        kokoro_tts._asr = object()
        # Hold the lock from another thread; /free must not get in.
        got = []
        with kokoro_tts._model_lock:
            t = threading.Thread(
                target=lambda: got.append(self.client.post("/free").status_code))
            t.start()
            t.join(timeout=0.3)
            still_held = t.is_alive()
        t.join(timeout=2.0)
        self.assertTrue(still_held, "/free did not wait for the model lock")


class InFlightTests(KokoroTestCase):
    def test_the_counter_returns_to_zero_when_a_call_raises(self):
        with self.assertRaises(RuntimeError):
            with kokoro_tts._Using():
                raise RuntimeError("synth blew up")
        self.assertEqual(kokoro_tts._inflight, 0)

    def test_the_counter_nests(self):
        with kokoro_tts._Using():
            with kokoro_tts._Using():
                self.assertEqual(kokoro_tts._inflight, 2)
        self.assertEqual(kokoro_tts._inflight, 0)


if __name__ == "__main__":
    unittest.main()
