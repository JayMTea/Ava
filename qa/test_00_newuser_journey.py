"""THE fresh-user story, in strict order, on a pristine AVA_HOME.

This file must sort first (test_00_*) so it sees the home before any other test
creates the password. It walks exactly what a brand-new user experiences:
redirect to /setup → password rules → onboarding wizard → dashboard →
first chat (served by the fake model) → the conversation persisted in
data/chats.db.
"""
import json
import os
import unittest

import pytest

from qa import helpers
from qa.conftest import FAKE_LLM, QA_HOME
from qa.env_recipe import QA_PASSWORD


@pytest.fixture(scope="module", autouse=True)
def _bridge(bridge):
    globals()["CLIENT"] = bridge  # unittest classes can't take fixtures directly
    yield


class TestNewUserJourney(unittest.TestCase):
    """Ordered steps — test methods sort alphabetically, hence the numbering."""

    def test_01_fresh_home_redirects_to_setup(self):
        c = CLIENT
        r = c.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/setup")
        # API callers get JSON 401, never an HTML redirect.
        r = c.get("/api/brand")
        self.assertEqual(r.status_code, 401)
        self.assertIn("error", r.json())
        # /login itself bounces to /setup while no password exists.
        r = c.get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/setup")

    def test_02_password_rules_enforced(self):
        c = CLIENT
        r = c.post("/setup", data={"password": "short", "confirm": "short"},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        r = c.post("/setup", data={"password": "long-enough-pw", "confirm": "different"},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 400)

    def test_03_set_password_enters_wizard(self):
        c = CLIENT
        r = c.post("/setup", data={"password": QA_PASSWORD, "confirm": QA_PASSWORD},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        # Fresh install -> onboarding wizard, and the session cookie is set.
        self.assertEqual(r.headers["location"], "/setup/wizard")
        self.assertTrue(os.path.isfile(os.path.join(QA_HOME, "data", "auth_password")))
        # The setup screen is one-shot: with a password set it bounces to /login.
        r = c.get("/setup", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")

    def test_04_wizard_steps(self):
        c = CLIENT
        r = c.get("/setup/wizard")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<html", r.text.lower())

        hw = c.get("/api/setup/hardware").json()
        self.assertIn("tier", hw)
        self.assertTrue(hw["tier"])  # a model-size tier ("large") or "cloud"

        # The probe used to answer {vllm, ollama, router} from two hardcoded
        # loopback addresses. Inside the Docker image that is the CONTAINER's
        # loopback, where the compose engines (vllm:8002 / ollama:11434) are not
        # — so every Docker first-run reported "no local engine" and the wizard
        # preselected cloud for someone who had just started a local one. It now
        # returns the candidates it actually probed, in priority order.
        be = c.get("/api/setup/backends").json()
        self.assertEqual(set(be) >= {"backends", "any_up", "router"}, True)
        self.assertTrue(be["backends"], "no engine candidates were probed at all")
        for cand in be["backends"]:
            self.assertEqual(set(cand) >= {"id", "base_url", "engine", "up"}, True)
        bases = [x["base_url"] for x in be["backends"]]
        self.assertIn("http://vllm:8002/v1", bases)      # the compose service…
        self.assertIn("http://127.0.0.1:8002/v1", bases)  # …and bare metal

        cons = c.get("/api/setup/connectors").json()["connectors"]
        self.assertTrue(any(x["id"] == "bridge" for x in cons))

        r = c.post("/api/setup/save", json={
            "inference": {"mode": "local", "engine": "vllm",
                          "base_url": "http://127.0.0.1:9999/v1", "model": "qa-model"},
            "features": {"voice": False, "web_search": False, "image": True},
            "connectors": [],
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        # setup.completed persisted…
        from ava_bridge import setup_wizard
        self.assertTrue(setup_wizard.setup_completed())
        # …but the wizard stays REACHABLE. It used to 303 away once complete,
        # which combined badly with api_save marking completion before it
        # validated: a blank model field returned {"ok": true}, set the flag,
        # wrote no backend, and then locked the only screen that could fix it.
        # Re-entrant means the worst case is landing here again.
        r = c.get("/setup/wizard", follow_redirects=False)
        self.assertEqual(r.status_code, 200)

    def test_04b_a_blank_model_is_refused_and_claims_nothing(self):
        """The defect above, asserted directly: an empty field must not be able
        to mark onboarding done."""
        c = CLIENT
        before = c.get("/api/setup/backends").status_code
        self.assertEqual(before, 200)
        r = c.post("/api/setup/save", json={
            "inference": {"mode": "local", "engine": "vllm",
                          "base_url": "http://127.0.0.1:9999/v1", "model": ""},
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json().get("field"), "model")

    def test_05_dashboard_shell_loads(self):
        c = CLIENT
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        brand = c.get("/api/brand").json()
        self.assertTrue(brand.get("name"))
        self.assertEqual(c.get("/api/health").status_code, 200)

    def test_06_first_chat_round_trip(self):
        c = CLIENT
        FAKE_LLM.reply = "Hi! I'm your local assistant — everything stays on this machine."
        chat = c.post("/api/chats").json()
        cid = chat["id"]
        r = c.post("/api/talk-text", data={"text": "Hello, are you working?",
                                           "chat_id": cid})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("local assistant", body["reply"])
        # The model actually received the user's words via the direct floor.
        sent = json.dumps(FAKE_LLM.requests[-1]["body"])
        self.assertIn("Hello, are you working?", sent)

        # And the conversation is durable: in the API and on disk under AVA_HOME.
        msgs = c.get(f"/api/chats/{cid}").json()["messages"]
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles[:2], ["user", "assistant"])
        # Durable on disk too. The corpus lives in data/chats.db now, not
        # chats.json — asserted by reopening the store from the file rather than
        # by trusting the API that just answered, so this still fails if the
        # write never reached disk.
        import sqlite3
        db = os.path.join(QA_HOME, "data", "chats.db")
        self.assertTrue(os.path.isfile(db), "chats.db was never written")
        con = sqlite3.connect(db)
        try:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM chats WHERE id=?", (cid,)).fetchone()[0],
                1, "the conversation is not in the store")
            self.assertGreaterEqual(
                con.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?",
                            (cid,)).fetchone()[0], 2,
                "the user turn and the reply were not both persisted")
        finally:
            con.close()

    def test_07_logout_and_login_again(self):
        c = CLIENT
        r = c.post("/logout", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(c.get("/api/brand").status_code, 401)
        # Wrong password: 401 + the form again.
        r = c.post("/login", data={"password": "wrong-password"},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 401)
        # Right password: back in.
        helpers.ensure_authed(c)
        self.assertEqual(c.get("/api/brand").status_code, 200)
