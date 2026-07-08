"""Long-term memory: store, retrieval, recall folding, distiller, hub surface.

Covers the claims in docs/MEMORY.md: FTS5 store under $AVA_HOME (0600),
dedupe, document chunk indexing with re-upload replacement, lexical recall
with a bounded block, audit-logged recall, and the distiller's parsing +
cursor behavior.
"""
import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

# Isolate AVA_HOME so the store/ledger never touch the real one. NOTE: this
# alone is not enough in a full-suite run — another test module may import
# ava_bridge.settings first, freezing AVA_HOME at the repo root — so every
# test class below ALSO patches memory_store.db_path (see _MemCase).
os.environ.setdefault("AVA_HOME", tempfile.mkdtemp())

from ava_bridge import audit, chat_store, config, learning, memory_store, state  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="ava_memtest_")
_DB = os.path.join(_TMP, "memory.db")


class _MemCase(unittest.TestCase):
    """Base: point the store at a private temp db regardless of import order."""

    def setUp(self):
        self._dbp = patch.object(memory_store, "db_path", lambda: _DB)
        self._dbp.start()
        try:
            os.remove(_DB)
        except FileNotFoundError:
            pass

    def tearDown(self):
        self._dbp.stop()


class TestStore(_MemCase):
    def test_add_dedupe_update_delete(self):
        i1 = memory_store.add("fact", "Owner prefers metric units.", source="manual")
        self.assertIsInstance(i1, int)
        # Same kind+text refreshes, never duplicates.
        self.assertEqual(memory_store.add("fact", "Owner prefers metric units."), i1)
        self.assertEqual(memory_store.counts()["facts"], 1)
        self.assertTrue(memory_store.update_item(i1, text="Owner prefers imperial units."))
        self.assertIn("imperial", memory_store.list_items()[0]["text"])
        self.assertTrue(memory_store.delete_item(i1))
        self.assertFalse(memory_store.delete_item(i1))  # already gone
        self.assertEqual(memory_store.counts()["total"], 0)

    def test_rejects_junk(self):
        self.assertIsNone(memory_store.add("fact", "   "))
        self.assertIsNone(memory_store.add("weird_kind", "text"))

    def test_file_is_0600(self):
        memory_store.add("fact", "perm probe")
        self.assertEqual(os.stat(memory_store.db_path()).st_mode & 0o777, 0o600)

    def test_pinned_ranks_first(self):
        a = memory_store.add("fact", "The garage server is a Jetson Orin.")
        b = memory_store.add("fact", "The Jetson in the garage runs the sprinkler system.")
        memory_store.update_item(b, pinned=True)
        hits = memory_store.search("jetson garage")
        self.assertEqual(hits[0]["id"], b)
        self.assertEqual({h["id"] for h in hits}, {a, b})

    def test_fts_query_sanitizes(self):
        # Raw punctuation is not valid MATCH syntax — must not raise.
        memory_store.add("fact", "Deploy uses docker compose on port 8096.")
        hits = memory_store.search('what\'s the "deploy" (docker?) setup!!')
        self.assertTrue(hits and "docker" in hits[0]["text"])
        # Pure stopwords -> no query -> no hits, no error.
        self.assertEqual(memory_store.search("what is the and for"), [])


class TestDocuments(_MemCase):
    def test_chunking(self):
        text = "\n\n".join(f"Paragraph {i} " + "x" * 300 for i in range(10))
        chunks = memory_store.chunk_text(text, size=1200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 2400 for c in chunks))
        # Hard-split pathological single paragraph.
        self.assertGreater(len(memory_store.chunk_text("y" * 10000, size=1200)), 3)

    def test_index_and_replace(self):
        n = memory_store.index_document("a1", "roadmap.txt",
                                        "Ship the PWA.\n\nThen governed memory.")
        self.assertGreater(n, 0)
        before = memory_store.counts()["doc_chunks"]
        # Re-upload same filename replaces, not accumulates.
        memory_store.index_document("a2", "roadmap.txt",
                                    "Ship the PWA.\n\nThen governed memory. v2")
        self.assertEqual(memory_store.counts()["doc_chunks"], before)
        hits = memory_store.search("governed memory roadmap", kind="doc")
        self.assertTrue(hits and hits[0]["text"].startswith("[roadmap.txt]"))

    def test_disabled_skips_indexing(self):
        with patch.object(config, "MEMORY_ENABLED", False):
            self.assertEqual(memory_store.index_document("a", "f.txt", "hello world"), 0)


class TestRecall(_MemCase):
    def test_recall_block_and_budget(self):
        memory_store.add("fact", "Owner's dog is named Biscuit, a corgi.")
        block, used = memory_store.recall("what's my dog's name?")
        self.assertIn("Biscuit", block)
        self.assertIn("[Long-term memory", block)
        self.assertEqual(len(used), 1)
        # Tight budget yields nothing rather than a broken block.
        block2, used2 = memory_store.recall("dog name", max_chars=3)
        self.assertEqual((block2, used2), ("", []))

    def test_augment_disabled_and_no_hits(self):
        with patch.object(config, "MEMORY_ENABLED", False):
            self.assertEqual(memory_store.augment_with_recall("t", "anything"), "t")
        self.assertEqual(memory_store.augment_with_recall("t", "zqxjkw unmatched"), "t")

    def test_augment_folds_and_audits(self):
        memory_store.add("fact", "Owner's dog is named Biscuit, a corgi.")
        ledger = os.path.join(_TMP, "audit.jsonl")
        with patch.object(audit, "_path", lambda: ledger):
            out = memory_store.augment_with_recall("original text", "my dog?", chat_id="c9")
            self.assertTrue(out.startswith("original text\n\n["))
            self.assertIn("Biscuit", out)
            evts = [e for e in audit.tail(20, kind="memory_recall")
                    if e.get("chat_id") == "c9"]
        self.assertTrue(evts and evts[0]["count"] == 1 and evts[0]["ids"])


class TestDistiller(_MemCase):
    def setUp(self):
        super().setUp()
        with state.chats_lock:
            state.chats.clear()

    def _seed_chat(self, n=6):
        now = time.time()
        with state.chats_lock:
            state.chats["c1"] = {"id": "c1", "title": "t", "created": now, "updated": now,
                                 "messages": [
                                     {"role": "user" if i % 2 == 0 else "assistant",
                                      "content": f"message {i} about the workshop jetson",
                                      "ts": now - 100 + i}
                                     for i in range(n)]}

    def test_recent_messages(self):
        self._seed_chat()
        msgs = chat_store.recent_messages(0)
        self.assertEqual(len(msgs), 6)
        self.assertEqual(msgs[0]["chat_id"], "c1")
        self.assertEqual(msgs, sorted(msgs, key=lambda m: m["ts"]))
        self.assertEqual(chat_store.recent_messages(time.time() + 10), [])

    def test_parse_facts(self):
        p = learning.MemoryDistiller._parse_facts
        self.assertEqual(p('noise ["a fact", "b fact"] more'), ["a fact", "b fact"])
        self.assertEqual(p("no json here"), [])
        self.assertEqual(p('[1, {"x": 2}, "keep"]'), ["keep"])

    def test_run_cycle_stores_and_advances_cursor(self):
        self._seed_chat()

        async def fake_llm(prompt, max_tokens):
            return '["Owner has a Jetson in the workshop.", "Owner likes corgis."]'

        with patch.object(learning, "_complete_local", fake_llm):
            n = asyncio.run(learning.memory_distiller.run_cycle())
        self.assertEqual(n, 2)
        self.assertEqual(memory_store.counts()["facts"], 2)
        # Cursor advanced: second run sees nothing new, makes no LLM call.
        async def boom(prompt, max_tokens):
            raise AssertionError("should not be called")
        with patch.object(learning, "_complete_local", boom), \
             patch.object(learning, "_complete_anthropic", boom):
            self.assertEqual(asyncio.run(learning.memory_distiller.run_cycle()), 0)

    def test_run_cycle_keeps_cursor_on_llm_failure(self):
        self._seed_chat()

        async def dead(prompt, max_tokens):
            return None

        with patch.object(learning, "_complete_local", dead), \
             patch.object(learning, "_complete_anthropic", dead):
            self.assertEqual(asyncio.run(learning.memory_distiller.run_cycle()), 0)
        # Messages were NOT consumed — a later cycle can retry them.
        self.assertEqual(memory_store.kv_get(learning.MemoryDistiller.KV_KEY, "0"), "0")


class TestHubSurface(_MemCase):
    def test_list_and_export(self):
        from ava_bridge import hub_api
        memory_store.add("fact", "exportable fact")
        r = hub_api.memory_list()
        self.assertEqual(r["counts"]["facts"], 1)
        self.assertTrue(r["items"][0]["text"] == "exportable fact")
        self.assertEqual(hub_api.memory_list(kind="nope").status_code, 400)
        data = memory_store.export_all()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["text"], "exportable fact")

    def test_delete_route(self):
        from ava_bridge import hub_api
        mid = memory_store.add("fact", "to be forgotten")
        self.assertEqual(hub_api.memory_delete(mid), {"ok": True})
        self.assertEqual(hub_api.memory_delete(mid).status_code, 404)


if __name__ == "__main__":
    unittest.main()
