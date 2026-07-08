"""Model bench/compare + durable chain-of-thought — the two deferred features."""
import json
import unittest
from unittest import mock

from ava_bridge import bench
from ava_bridge.chat_store import _trim_steps


class _FakeStream:
    def __init__(self, chunks, usage_tokens=None):
        self._chunks = chunks
        self._usage = usage_tokens
        self.status_code = 200

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def iter_lines(self):
        for c in self._chunks:
            yield ("data: " + json.dumps({"choices": [{"delta": {"content": c}}]})).encode()
        if self._usage is not None:
            yield ("data: " + json.dumps(
                {"choices": [{"delta": {}}], "usage": {"completion_tokens": self._usage}})).encode()
        yield b"data: [DONE]"


class TestBench(unittest.TestCase):
    def _b(self, i="m1"):
        return {"id": i, "url": "http://x/v1", "model": i, "engine": "vllm"}

    def test_reported_usage(self):
        with mock.patch("requests.post", return_value=_FakeStream(["a", "b", "c"], usage_tokens=9)):
            r = bench.bench_one(self._b(), "hi", 50)
        self.assertTrue(r["ok"])
        self.assertEqual(r["tokens"], 9)
        self.assertFalse(r["estimated_tokens"])
        self.assertGreater(r["tok_s"], 0)

    def test_estimated_when_no_usage(self):
        with mock.patch("requests.post", return_value=_FakeStream(["x" * 40])):
            r = bench.bench_one(self._b(), "hi", 50)
        self.assertTrue(r["ok"])
        self.assertTrue(r["estimated_tokens"])   # ~chars/4
        self.assertEqual(r["tokens"], 10)

    def test_dead_backend_no_raise(self):
        with mock.patch("requests.post", side_effect=Exception("refused")):
            r = bench.bench_one(self._b(), "hi", 50)
        self.assertFalse(r["ok"])
        self.assertIn("refused", r["error"])

    def test_bench_picks_winner_and_filters(self):
        bs = [self._b("fast"), self._b("slow")]
        # fast returns more tokens in the same wall time -> higher tok/s
        def fake(*a, **k):
            body = k.get("json") or {}
            n = 20 if body.get("model") == "fast" else 4
            return _FakeStream(["z"], usage_tokens=n)
        with mock.patch.object(bench, "backends", return_value=bs), \
             mock.patch("requests.post", side_effect=fake):
            res = bench.bench("hi", max_tokens=50)
            self.assertEqual(res["winner"], "fast")
            # filter to one backend
            res2 = bench.bench("hi", only=["slow"], max_tokens=50)
            self.assertEqual([r["id"] for r in res2["results"]], ["slow"])


class TestDurableCoT(unittest.TestCase):
    def test_trim_caps_and_preserves(self):
        steps = [{"kind": "thinking", "text": "y" * 9000},
                 {"kind": "tool", "name": "get_weather"},
                 {"kind": "text", "text": "ok"}]
        t = _trim_steps(steps)
        self.assertEqual(len(t), 3)
        self.assertTrue(t[0]["text"].endswith("[truncated]"))
        self.assertLess(len(t[0]["text"]), 4100)
        self.assertEqual(t[1]["name"], "get_weather")
        self.assertNotIn("text", t[1])

    def test_trim_empty(self):
        self.assertIsNone(_trim_steps(None))
        self.assertIsNone(_trim_steps([]))

    def test_trim_caps_count(self):
        many = [{"kind": "text", "text": str(i)} for i in range(200)]
        self.assertEqual(len(_trim_steps(many)), 60)


if __name__ == "__main__":
    unittest.main()
