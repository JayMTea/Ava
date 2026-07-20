"""Device connectors — the inbound "app → Ava" push channel: per-connector
bearer tokens, event storage/rotation, flood protection, and the Hub verify
step ("waiting for the first reading…").
"""
import os
import unittest

import pytest

from qa import helpers
from qa.conftest import QA_HOME

CID = "qasensor"


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    c = bridge
    r = c.post("/api/hub/connectors/new", json={
        "id": CID, "label": "QA Sensor", "kind": "app",
        "role": "device", "ingest": True})
    assert r.status_code in (200, 409), r.text
    helpers.refresh_connectors()
    yield


class TestDeviceIngest(unittest.TestCase):
    def test_hub_mints_the_ingest_token(self):
        r = CLIENT.get(f"/api/hub/connectors/{CID}/ingest-token")
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["token"])
        self.assertTrue(body["enabled"])
        self.assertEqual(body["url"], f"/api/connectors/{CID}/events")

    def test_event_push_requires_the_right_bearer(self):
        c = CLIENT
        ev = {"name": "qa-temp", "kind": "reading",
              "message": "qa temperature 21.5C"}
        # No token / wrong token / ANOTHER connector's token: all rejected.
        self.assertEqual(c.post(f"/api/connectors/{CID}/events", json=ev,
                                headers={}).status_code, 401)
        self.assertEqual(c.post(f"/api/connectors/{CID}/events", json=ev,
                                headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(c.post(f"/api/connectors/{CID}/events", json=ev,
                                headers=helpers.ingest_bearer("otherapp")).status_code, 401)
        # The connector's own token works — WITHOUT a session cookie (device
        # apps are not browsers).
        anon = helpers.anon_client()
        r = anon.post(f"/api/connectors/{CID}/events", json=ev,
                      headers=helpers.ingest_bearer(CID))
        self.assertEqual(r.status_code, 200, r.text)

    def test_event_lands_in_feed_and_on_disk(self):
        c = CLIENT
        c.post(f"/api/connectors/{CID}/events",
               json={"name": "qa-temp", "kind": "reading",
                     "message": "qa reading 42"},
               headers=helpers.ingest_bearer(CID))
        last = c.get(f"/api/hub/connectors/{CID}/last-event").json()
        self.assertTrue(last["ok"])
        self.assertIsNotNone(last["event"], "no event recorded")
        stream = os.path.join(QA_HOME, "logs", "devices", f"{CID}.jsonl")
        self.assertTrue(os.path.isfile(stream), stream)
        devices = c.get("/api/devices").json()
        self.assertIn(CID, str(devices))

    def test_flood_is_rate_limited(self):
        anon = helpers.anon_client()
        headers = helpers.ingest_bearer(CID)
        codes = []
        for i in range(200):
            r = anon.post(f"/api/connectors/{CID}/events",
                          json={"name": "qa-temp", "kind": "reading",
                                "message": f"burst {i}"},
                          headers=headers)
            codes.append(r.status_code)
            if r.status_code == 429:
                break
        self.assertIn(429, codes, "token bucket never engaged after 200 events")

    def test_unknown_connector_rejected(self):
        r = CLIENT.post("/api/connectors/ghost/events", json={"kind": "x"},
                        headers=helpers.ingest_bearer("ghost"))
        self.assertEqual(r.status_code, 404)
