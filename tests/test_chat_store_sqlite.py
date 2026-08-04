"""The SQLite chat store must preserve history exactly, including the migration.

This is the test that matters most in the storage swap: the thing being moved is
someone's conversation history, and a lossy migration is not recoverable from the
inside — it looks like success.

So the central check is a ROUND TRIP against a corpus containing every message
shape the real store has produced (attachments, model metadata, tools_used,
trimmed reasoning steps, error codes) plus a field this code has never heard of.
That last one is the point of the `extra` JSON column: message shape has grown
repeatedly, and a store that only preserves the fields it knows about silently
drops the next one.
"""
import json
import os
import tempfile
import unittest
from unittest import mock


def _fresh_store():
    """A chat_store bound to an empty temp data dir, reimported clean."""
    import importlib
    from ava_bridge import chat_store, settings
    d = tempfile.mkdtemp()
    p = mock.patch.object(settings, "data_dir", return_value=d)
    p.start()
    importlib.reload(chat_store)
    return chat_store, d, p


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.cs, self.dir, self._p = _fresh_store()
        self.addCleanup(self._p.stop)
        self.cs.reset()

    def test_every_message_field_survives_a_round_trip(self):
        cid = self.cs.chat_new("t")["id"]
        rich = {
            "atts": [{"filename": "a.pdf", "kind": "doc", "url": "/u/a.pdf"}],
            "model": {"id": "m", "label": "M"},
            "tools_used": ["web_fetch"],
            "error_code": "web_search_off",
        }
        self.cs.chat_append(cid, "assistant", "hello", ts=1000.0, **rich)
        got = self.cs.get(cid)["messages"][0]
        for k, v in rich.items():
            self.assertEqual(got[k], v, f"{k} did not survive")
        self.assertEqual(got["ts"], 1000.0)
        self.assertEqual(got["role"], "assistant")
        self.assertEqual(got["content"], "hello")

    def test_explicit_timestamps_are_kept_not_restamped(self):
        """A migration that restamps makes a whole corpus happen at once."""
        cid = self.cs.chat_new("t")["id"]
        for i in range(3):
            self.cs.chat_append(cid, "user", f"m{i}", ts=500.0 + i)
        self.assertEqual([m["ts"] for m in self.cs.get(cid)["messages"]],
                         [500.0, 501.0, 502.0])

    def test_append_order_survives_a_tied_timestamp(self):
        """Two messages can share a ts; (ts, id) keeps them in insert order."""
        cid = self.cs.chat_new("t")["id"]
        for i in range(5):
            self.cs.chat_append(cid, "user", f"m{i}", ts=42.0)
        self.assertEqual([m["content"] for m in self.cs.get(cid)["messages"]],
                         ["m0", "m1", "m2", "m3", "m4"])

    def test_history_for_returns_the_tail_oldest_first(self):
        cid = self.cs.chat_new("t")["id"]
        for i in range(30):
            self.cs.chat_append(cid, "user", f"m{i}", ts=float(i))
        h = self.cs.history_for(cid, limit=5)
        self.assertEqual([m["content"] for m in h], ["m25", "m26", "m27", "m28", "m29"])

    def test_delete_removes_the_messages_too(self):
        cid = self.cs.chat_new("t")["id"]
        self.cs.chat_append(cid, "user", "x")
        self.assertIsNotNone(self.cs.delete(cid))
        self.assertIsNone(self.cs.get(cid))
        self.assertEqual(self.cs.counts(), (0, 0))

    def test_recent_messages_spans_chats_and_filters_by_ts(self):
        a = self.cs.chat_new("a")["id"]
        b = self.cs.chat_new("b")["id"]
        self.cs.chat_append(a, "user", "old", ts=10.0)
        self.cs.chat_append(b, "user", "new", ts=20.0)
        got = self.cs.recent_messages(15.0)
        self.assertEqual([m["content"] for m in got], ["new"])
        self.assertEqual(got[0]["chat_id"], b)


class LegacyMigrationTests(unittest.TestCase):
    """Importing data/chats.json — once, faithfully, and never destructively."""

    def _legacy(self, tmp, corpus):
        path = os.path.join(tmp, "chats.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(corpus, f)
        return path

    def _store_with_legacy(self, corpus):
        import importlib
        from ava_bridge import chat_store, config, settings
        d = tempfile.mkdtemp()
        path = self._legacy(d, corpus)
        p1 = mock.patch.object(settings, "data_dir", return_value=d)
        p2 = mock.patch.object(config, "CHATS_FILE", path)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        importlib.reload(chat_store)   # triggers the one-time import
        return chat_store, path

    CORPUS = {
        "c1": {"id": "c1", "title": "First", "created": 100.0, "updated": 140.0,
               "messages": [
                   {"role": "user", "content": "hi", "ts": 110.0},
                   {"role": "assistant", "content": "yo", "ts": 120.0,
                    "model": {"id": "m"}, "tools_used": ["web_fetch"]},
                   {"role": "assistant", "content": "here it is", "ts": 140.0,
                    "atts": [{"filename": "a.pdf", "kind": "doc"}],
                    "a_future_field": {"nested": [1, 2]}},
               ]},
        "c2": {"id": "c2", "title": "Second", "created": 200.0, "updated": 200.0,
               "messages": []},
    }

    def test_migration_is_lossless_including_unknown_fields(self):
        cs, _ = self._store_with_legacy(self.CORPUS)
        self.assertEqual(cs.counts(), (2, 3))
        msgs = cs.get("c1")["messages"]
        self.assertEqual([m["ts"] for m in msgs], [110.0, 120.0, 140.0])
        self.assertEqual(msgs[1]["tools_used"], ["web_fetch"])
        self.assertEqual(msgs[2]["atts"], [{"filename": "a.pdf", "kind": "doc"}])
        # The field this code has never heard of — what `extra` exists for.
        self.assertEqual(msgs[2]["a_future_field"], {"nested": [1, 2]})
        c2 = cs.get("c2")
        self.assertEqual(c2["title"], "Second")
        self.assertEqual(c2["messages"], [])

    def test_legacy_file_is_left_on_disk_as_a_rollback(self):
        cs, path = self._store_with_legacy(self.CORPUS)
        self.assertTrue(os.path.isfile(path), "chats.json must survive the migration")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 2)

    def test_import_does_not_repeat_and_does_not_resurrect_deletions(self):
        """Guarded on the table being empty, not on the file existing.

        Otherwise deleting every conversation would bring them all back on the
        next boot, since the rollback file is still sitting there.
        """
        import importlib
        cs, _ = self._store_with_legacy(self.CORPUS)
        self.assertEqual(cs.counts()[0], 2)
        cs.delete("c1")
        cs.delete("c2")
        self.assertEqual(cs.counts(), (0, 0))
        importlib.reload(cs)
        self.assertEqual(cs.counts(), (0, 0),
                         "deleting every chat must not be undone by a restart: "
                         "chats.json is still on disk as the rollback, so an "
                         "emptiness-guarded import would resurrect the corpus")

    def test_export_json_reproduces_the_legacy_shape(self):
        cs, _ = self._store_with_legacy(self.CORPUS)
        out = json.loads(cs.export_json())
        self.assertEqual(set(out), {"c1", "c2"})
        self.assertEqual(set(out["c1"]), {"id", "title", "created", "updated", "messages"})
        self.assertEqual(len(out["c1"]["messages"]), 3)
