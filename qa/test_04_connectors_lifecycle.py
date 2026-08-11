"""The full connector lifecycle a real user drives from the Hub, against a
real (fake) app: probe → create → visible everywhere → tools discovered →
actions proxied (with JIT consent) → perf recorded into Vitals → disable →
delete → HISTORY SURVIVES → reconnect resumes it.
"""
import os
import unittest

import pytest

from qa import helpers
from qa.conftest import FAKE_APP, QA_HOME

CID = "qaapp"


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    yield


class TestConnectorLifecycle(unittest.TestCase):
    """Ordered: each step builds on the previous (one story, like the user's)."""

    def test_01_probe_discovers_the_app(self):
        r = CLIENT.post("/api/hub/connectors/probe",
                        json={"url": FAKE_APP.url})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"), body)

    def test_02_create_connector(self):
        r = CLIENT.post("/api/hub/connectors/new", json={
            "id": CID, "label": "QA App", "kind": "app",
            "base_url": FAKE_APP.url,
            "probe": FAKE_APP.url + "/health",
            "actions": [
                {"id": "ping", "method": "GET", "path": "/api/ping",
                 "description": "read something", "access": "read"},
                {"id": "echo", "method": "POST", "path": "/api/echo",
                 "description": "write something", "access": "write"},
            ],
        })
        self.assertEqual(r.status_code, 200, r.text)
        manifest = r.json()["manifest"]
        # Deploy-time defaults: a perf source OUTSIDE the connector dir.
        self.assertEqual(manifest["perf"]["app"], CID)
        self.assertIn("${AVA_LOGS}/apps/", manifest["perf"]["path"])
        path = os.path.join(QA_HOME, "connectors", CID, "connector.yaml")
        self.assertTrue(os.path.isfile(path))
        # Duplicate id is refused, not overwritten.
        r = CLIENT.post("/api/hub/connectors/new", json={"id": CID, "label": "x"})
        self.assertEqual(r.status_code, 409)

    def test_03_appears_everywhere_immediately(self):
        helpers.refresh_connectors()
        c = CLIENT
        ops = c.get("/api/ops/connectors").json()["connectors"]
        row = next((x for x in ops if x["id"] == CID), None)
        self.assertIsNotNone(row, f"{CID} not in ops connectors")
        self.assertTrue(row["has_perf"])
        self.assertEqual(row["perf_app"], CID)
        # Vitals perf sources know it without any restart.
        summary = c.get("/api/perf/summary").json()
        self.assertIn(CID, summary["apps"])
        # Hub management list has it too.
        hub = c.get("/api/hub/connectors").json()
        self.assertIn(CID, str(hub))

    def test_04_read_action_proxies_and_records_perf(self):
        c = CLIENT
        r = c.post(f"/internal/connector/{CID}/ping",
                   headers=helpers.internal_headers(), json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("pong"))
        self.assertTrue(any(x["path"].startswith("/api/ping") for x in FAKE_APP.calls))
        # The bridge observed the call into the app's perf log (Vitals data).
        perf_file = os.path.join(QA_HOME, "logs", "apps", CID, "performance.jsonl")
        self.assertTrue(os.path.isfile(perf_file), perf_file)
        summary = c.get(f"/api/perf/summary?app={CID}").json()
        self.assertTrue(summary["ok"], summary)
        self.assertGreaterEqual(summary["summary"].get("actions", {}).get("count", 0), 1)

    def test_05_write_action_parks_for_approval(self):
        """approvals.gate BLOCKS the call until the operator decides, so the
        proxied call runs in a thread while this test plays the operator."""
        import threading
        import time as _time
        c = CLIENT
        FAKE_APP.reset()
        result = {}

        def call():
            result["r"] = c.post(f"/internal/connector/{CID}/echo",
                                 headers=helpers.internal_headers(),
                                 json={"v": "blocked?"})

        t = threading.Thread(target=call, daemon=True)
        t.start()
        # The request parks: it shows up as a pending approval.
        aid = None
        deadline = _time.time() + 10
        while _time.time() < deadline and aid is None:
            pending = c.get("/api/hub/approvals").json()["pending"]
            for p in pending:
                if p["connector"] == CID and p["action"] == "echo":
                    aid = p["id"]
            _time.sleep(0.1)
        self.assertIsNotNone(aid, "write action never parked for approval")
        self.assertFalse(any(x["path"].startswith("/api/echo")
                             for x in FAKE_APP.calls), "write ran without consent!")
        # Operator denies -> the caller gets a refusal, the app is never hit.
        r = c.post(f"/api/hub/approvals/{aid}", params={"decision": "deny"})
        self.assertTrue(r.json().get("ok"), r.text)
        t.join(timeout=15)
        # HTTP 200 with a coded refusal, NOT a 403. The caller here is a sandboxed
        # tool going through `_lib.mjs`, which uses `curl --fail` — a 403 makes
        # curl exit 22 and discard the body, so "you denied this" reached Ava as
        # `request failed: exit status 22` and she could not tell the owner what
        # had happened or why. The refusal is the payload; see internal._told().
        self.assertEqual(result["r"].status_code, 200, result["r"].text)
        body = result["r"].json()
        self.assertEqual(body["error_code"], "awaiting_approval")
        self.assertIn("denied", body["error"])
        self.assertFalse(any(x["path"].startswith("/api/echo") for x in FAKE_APP.calls),
                         "the app was called despite the denial")

    def test_06_grant_then_write_runs_silently(self):
        c = CLIENT
        FAKE_APP.reset()
        # Pre-grant from the settings page ("Always allow" equivalent).
        r = c.post(f"/api/hub/connectors/{CID}/grants/echo")
        self.assertTrue(r.json().get("ok"), r.text)
        r = c.post(f"/internal/connector/{CID}/echo",
                   headers=helpers.internal_headers(), json={"v": "now allowed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(any(x["path"].startswith("/api/echo") for x in FAKE_APP.calls))
        # The grants sheet reflects it; revoking flips it back.
        g = c.get(f"/api/hub/connectors/{CID}/grants").json()
        echo = next(a for a in g["actions"] if a["id"] == "echo")
        self.assertTrue(echo["granted"])
        c.delete(f"/api/hub/connectors/{CID}/grants/echo")
        g = c.get(f"/api/hub/connectors/{CID}/grants").json()
        echo = next(a for a in g["actions"] if a["id"] == "echo")
        self.assertFalse(echo["granted"])

    def test_07_generate_preview_and_manifest_edit(self):
        c = CLIENT
        gen = c.post(f"/api/hub/connectors/{CID}/generate").json()
        self.assertTrue(gen["ok"])
        self.assertTrue(gen["tools"], "no agent tools generated")
        m = c.get(f"/api/hub/connectors/{CID}/manifest").json()
        self.assertTrue(m["editable"])
        # Round-trip the YAML unchanged.
        r = c.post(f"/api/hub/connectors/{CID}/manifest", json={"yaml": m["yaml"]})
        self.assertTrue(r.json().get("ok"), r.text)
        # An id change is refused.
        bad = m["yaml"].replace(f"id: {CID}", "id: hijacked")
        r = c.post(f"/api/hub/connectors/{CID}/manifest", json={"yaml": bad})
        self.assertEqual(r.status_code, 400)

    def test_07b_written_artifacts_land_where_the_sandbox_installer_reads_them(self):
        """This tier stopped at `generate?write=0` — a preview — so nothing ever
        asserted that a real write lands somewhere `agent/install.sh` looks.

        It did not, for two of the three install shapes: the files went to
        CODE_ROOT, which is an image layer in Docker (discarded by `up --build`,
        keeping the manifests that produced them) and a SEPARATE copy in the
        agent container (so the sandbox installer could never read them). The
        symptom was an app that connected fine and simply never worked.
        """
        c = CLIENT
        r = c.post(f"/api/hub/connectors/{CID}/generate?write=1")
        gen = r.json()
        self.assertTrue(gen["ok"], r.text)
        self.assertTrue(gen["wrote"], "write=1 wrote nothing")

        tools = os.path.join(QA_HOME, "agent", "mcp_server_connectors", "apps", CID)
        policy = os.path.join(QA_HOME, "agent", "policies", "generated", f"{CID}.yaml")
        self.assertTrue(os.path.isdir(tools), f"no tools at {tools}")
        self.assertTrue([f for f in os.listdir(tools) if f.endswith(".mjs")],
                        f"tool directory is empty: {tools}")
        self.assertTrue(os.path.isfile(policy), f"no egress policy at {policy}")

        # Every path it reports is relative to AVA_HOME, so a reader can tell
        # which root it means on an install shape where the two differ.
        for rel in gen["wrote"]:
            self.assertFalse(os.path.isabs(rel), f"absolute path reported: {rel}")
            self.assertTrue(os.path.exists(os.path.join(QA_HOME, rel)),
                            f"reported writing {rel}, which is not under AVA_HOME")

        # And the policy allows exactly the routes the tools call — the lockstep
        # that deny-by-default makes load-bearing.
        pol = open(policy, encoding="utf-8").read()
        self.assertIn(f"/internal/connector/{CID}/", pol)

    def test_08_disable_hides_enable_restores(self):
        c = CLIENT
        c.post(f"/api/hub/connectors/{CID}/enabled", json={"enabled": False})
        helpers.refresh_connectors()
        ops = c.get("/api/ops/connectors").json()["connectors"]
        self.assertNotIn(CID, [x["id"] for x in ops])
        c.post(f"/api/hub/connectors/{CID}/enabled", json={"enabled": True})
        helpers.refresh_connectors()
        ops = c.get("/api/ops/connectors").json()["connectors"]
        self.assertIn(CID, [x["id"] for x in ops])

    def test_09_delete_keeps_history_reconnect_resumes(self):
        c = CLIENT
        perf_file = os.path.join(QA_HOME, "logs", "apps", CID, "performance.jsonl")
        tools = os.path.join(QA_HOME, "agent", "mcp_server_connectors", "apps", CID)
        policy = os.path.join(QA_HOME, "agent", "policies", "generated", f"{CID}.yaml")
        self.assertTrue(os.path.isdir(tools), "precondition: test_07b wrote these")
        r = c.post(f"/api/hub/connectors/{CID}/delete")
        self.assertTrue(r.json().get("ok"), r.text)
        helpers.refresh_connectors()

        # No residue. Generated tools with no manifest behind them get tarred
        # into the sandbox on the next provision, where no policy allows their
        # routes — so Ava is handed tools that deny-by-default guarantees will
        # fail, and nothing reports it. Nothing walked backwards from what
        # EXISTS to what is declared until `connectors.orphans()` did.
        self.assertFalse(os.path.exists(tools), f"tools survived delete: {tools}")
        self.assertFalse(os.path.exists(policy), f"policy survived delete: {policy}")
        self.assertNotIn(CID, [x["id"] for x in
                               c.get("/api/ops/connectors").json()["connectors"]])
        # THE history guarantee: the perf log outlives the connector, and
        # Vitals still reads it (remembered in the source ledger).
        self.assertTrue(os.path.isfile(perf_file), "history deleted with connector!")
        summary = c.get("/api/perf/summary").json()
        self.assertIn(CID, summary["apps"], "removed app vanished from Vitals")

        # Reconnect under the same id -> same file, history resumes.
        r = c.post("/api/hub/connectors/new", json={
            "id": CID, "label": "QA App again", "kind": "app",
            "base_url": FAKE_APP.url,
            "actions": [{"id": "ping", "method": "GET", "path": "/api/ping",
                         "access": "read"}]})
        self.assertEqual(r.status_code, 200, r.text)
        helpers.refresh_connectors()

        # A grant is a standing "always allow" for one action. It used to outlive
        # the connector, so re-adding the same id silently re-inherited every
        # permission the owner had ever given the OLD app of that name — a
        # different program, same id.
        g = c.get(f"/api/hub/connectors/{CID}/grants").json()
        self.assertFalse([a for a in g["actions"] if a.get("granted")],
                         "a reconnected app inherited the deleted one's grants")
        s = c.get(f"/api/perf/summary?app={CID}").json()
        self.assertGreaterEqual(s["summary"].get("actions", {}).get("count", 0), 1,
                                "pre-delete history did not resume after reconnect")

    def test_10_discover_tools_facade(self):
        c = CLIENT
        # A discover-mode connector wraps the app's own /tools + /call.
        r = c.post("/api/hub/connectors/new", json={
            "id": "qadisc", "label": "QA Discover", "kind": "app",
            "base_url": FAKE_APP.url,
            "discover": {"base": FAKE_APP.url, "list": "/tools", "call": "/call"}})
        self.assertEqual(r.status_code, 200, r.text)
        helpers.refresh_connectors()
        r = c.get("/internal/connector/qadisc/__tools",
                  headers=helpers.internal_headers())
        tools = r.json().get("tools") or []
        self.assertIn("lookup", [t.get("name") for t in tools])
        # Call a read tool through the policed __call route.
        r = c.post("/internal/connector/qadisc/__call",
                   headers=helpers.internal_headers(),
                   json={"name": "lookup", "arguments": {"q": "qa"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("tool"), "lookup")
        c.post("/api/hub/connectors/qadisc/delete")
