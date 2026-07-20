"""THE fresh-user story, in strict order, on a pristine AVA_HOME.

This file must sort first (test_00_*) so it sees the home before any other test
creates the password. It walks exactly what a brand-new user experiences:
redirect to /setup → password rules → onboarding wizard → dashboard →
first chat (served by the fake model) → the conversation persisted on disk.
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

        be = c.get("/api/setup/backends").json()
        self.assertEqual(set(be) >= {"vllm", "ollama", "router"}, True)

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
        # setup.completed persisted -> the wizard now redirects home.
        from ava_bridge import setup_wizard
        self.assertTrue(setup_wizard.setup_completed())
        r = c.get("/setup/wizard", follow_redirects=False)
        self.assertEqual(r.status_code, 303)

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
        on_disk = json.load(open(os.path.join(QA_HOME, "data", "chats.json"),
                                 encoding="utf-8"))
        self.assertIn(cid, json.dumps(on_disk))

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
