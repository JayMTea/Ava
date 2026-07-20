"""The continuous user's data: what accumulates shows in Data stores, exports
completely (and never leaks secrets), survives maintenance, and can be
inspected per chat. Retention is a governed knob (tested in hub settings).
"""
import io
import unittest
import zipfile

import pytest

from qa import helpers
from qa.conftest import FAKE_LLM


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    c = bridge
    # Accumulate: a few conversations + a memory + audit rows (deletes audit).
    FAKE_LLM.reply = "Noted."
    for i in range(3):
        chat = c.post("/api/chats").json()
        c.post("/api/talk-text", data={"text": f"remember thing {i}",
                                       "chat_id": chat["id"]})
    c.post("/api/hub/memory", json={"text": "QA lifecycle fact", "kind": "fact"})
    globals()["CHAT_ID"] = chat["id"]
    yield


class TestDataStores(unittest.TestCase):
    def test_stores_inventory_counts_accumulated_data(self):
        body = CLIENT.get("/api/data/stores").json()
        text = str(body)
        for expected in ("chats", "memory", "audit", "performance"):
            self.assertIn(expected, text.lower(), f"store '{expected}' missing")

    def test_chats_listing_and_export(self):
        c = CLIENT
        chats = c.get("/api/data/chats").json()
        self.assertIn(CHAT_ID, str(chats))
        as_json = c.get(f"/api/data/chats/{CHAT_ID}/export?format=json")
        self.assertEqual(as_json.status_code, 200)
        self.assertIn("remember thing", as_json.text)
        as_md = c.get(f"/api/data/chats/{CHAT_ID}/export?format=md")
        self.assertEqual(as_md.status_code, 200)
        self.assertIn("remember thing", as_md.text)

    def test_log_tails(self):
        for name in ("audit", "performance"):
            r = CLIENT.get(f"/api/data/logs/{name}/tail")
            self.assertEqual(r.status_code, 200, name)

    def test_maintenance_integrity_and_vacuum(self):
        c = CLIENT
        self.assertEqual(c.get("/api/data/maintenance").status_code, 200)
        r = c.post("/api/data/maintenance/integrity")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("error", str(r.json()).lower())
        self.assertEqual(c.post("/api/data/maintenance/vacuum").status_code, 200)


class TestFullExport(unittest.TestCase):
    def test_export_zip_is_complete_and_leaks_no_secrets(self):
        r = CLIENT.get("/api/data/export")
        self.assertEqual(r.status_code, 200)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        joined = " ".join(names)
        for expected in ("chats", "memory", "audit"):
            self.assertIn(expected, joined, f"{expected} missing from export")
        # The red line: secrets never leave the machine in an export.
        for forbidden in (".secret", ".internal_token", "auth_password",
                          "router_token", "inference_key"):
            self.assertNotIn(forbidden, joined, f"SECRET {forbidden} in export!")
        blob = b"".join(zf.read(n) for n in names
                        if n.endswith((".json", ".jsonl", ".yaml")))
        from ava_bridge import config
        self.assertNotIn(config.INTERNAL_TOKEN.encode(), blob,
                         "internal token value leaked into export payloads")
        # And the user's actual content is in there.
        self.assertIn(b"QA lifecycle fact", blob)
