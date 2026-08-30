"""Chat + turns, the app's core loop, end to end against the fake model:
typed chat, streaming turns with live polling, chat CRUD, uploads feeding a
turn, ghost discard, and the failure path (model down -> clean error, no hang).
"""
import io
import unittest

import pytest

from qa import helpers
from qa.conftest import FAKE_LLM


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    yield


class TestChatCrud(unittest.TestCase):
    def test_create_rename_delete(self):
        c = CLIENT
        chat = c.post("/api/chats").json()
        cid = chat["id"]
        self.assertIn(cid, [x["id"] for x in c.get("/api/chats").json()["chats"]])

        r = c.patch(f"/api/chats/{cid}", data={"title": "QA renamed"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "QA renamed")

        self.assertTrue(c.delete(f"/api/chats/{cid}").json()["ok"])
        self.assertEqual(c.get(f"/api/chats/{cid}").status_code, 404)
        # Deleting twice is a clean no-op, not an error.
        self.assertFalse(c.delete(f"/api/chats/{cid}").json()["ok"])
        # Deletion leaves an audit trace (governed data).
        events = c.get("/api/hub/audit?kind=chat_delete").json()["events"]
        self.assertTrue(any(e.get("id") == cid for e in events))

    def test_unknown_chat_404s(self):
        self.assertEqual(CLIENT.get("/api/chats/nope").status_code, 404)
        self.assertEqual(
            CLIENT.patch("/api/chats/nope", data={"title": "x"}).status_code, 404)

    def test_empty_message_rejected(self):
        r = CLIENT.post("/api/talk-text", data={"text": "   "})
        self.assertEqual(r.status_code, 400)


class TestTurns(unittest.TestCase):
    def test_streaming_turn_completes(self):
        c = CLIENT
        FAKE_LLM.reply = "Streaming turn reply from the QA model."
        chat = c.post("/api/chats").json()
        r = c.post("/api/chat-stream", data={"text": "stream please",
                                             "chat_id": chat["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["turn_id"]
        turn = helpers.wait_turn(c, tid)
        self.assertEqual(turn["status"], "done", turn)
        self.assertIn("QA model", turn["reply"])
        # The turn also appears in the live turn feed the Agent console reads.
        feed = c.get("/api/turns").json()["turns"]
        self.assertIn(tid, [t["id"] for t in feed])

    def test_model_down_yields_error_not_hang(self):
        c = CLIENT
        # fail_all (not fail_next): the intent gate's classify call now precedes
        # the turn and resiliently absorbs a single failure, so "model down"
        # must fail every call for the TURN itself to surface the error.
        FAKE_LLM.fail_all = 500
        try:
            chat = c.post("/api/chats").json()
            r = c.post("/api/chat-stream", data={"text": "this one fails",
                                                 "chat_id": chat["id"]})
            tid = r.json()["turn_id"]
            turn = helpers.wait_turn(c, tid)
        finally:
            FAKE_LLM.fail_all = None
        self.assertEqual(turn["status"], "error")
        self.assertTrue(turn.get("error"))

    def test_talk_text_model_down_is_502(self):
        FAKE_LLM.fail_next = 500
        r = CLIENT.post("/api/talk-text", data={"text": "fails fast"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("error", r.json())

    def test_unknown_turn_404(self):
        self.assertEqual(CLIENT.get("/api/turn/does-not-exist").status_code, 404)

    def test_ghost_discard(self):
        r = CLIENT.post("/api/ghost/discard", data={"chat_id": "ghost-qa-1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("ok", r.json())


class TestUploads(unittest.TestCase):
    def test_text_upload_extracts_and_feeds_turn(self):
        c = CLIENT
        content = b"QA document: the launch code is PINEAPPLE-42."
        r = c.post("/api/upload", files={
            "files": ("qa-notes.txt", io.BytesIO(content), "text/plain")})
        self.assertEqual(r.status_code, 200, r.text)
        att = r.json()["attachments"][0]
        self.assertEqual(att["kind"], "document")
        self.assertGreater(att["chars"], 0)

        # The attachment's text is folded into the next turn's prompt.
        FAKE_LLM.reset()
        r = c.post("/api/talk-text", data={
            "text": "what does my note say?",
            "attachments": f'["{att["id"]}"]'})
        self.assertEqual(r.status_code, 200, r.text)
        import json as _json
        sent = _json.dumps(FAKE_LLM.requests[-1]["body"])
        self.assertIn("PINEAPPLE-42", sent)

    def test_disallowed_extension_refused(self):
        r = CLIENT.post("/api/upload", files={
            "files": ("evil.sh", io.BytesIO(b"#!/bin/sh\n"), "text/x-sh")})
        self.assertEqual(r.status_code, 200)
        out = r.json()["attachments"][0]
        self.assertIn("error", out)
        self.assertIn("not allowed", out["error"])
