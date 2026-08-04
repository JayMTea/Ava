"""Graceful degradation — what a fresh user WITHOUT the optional stacks
experiences. Every absent dependency must be a clear state, never a 500.
"""
import io
import unittest

import pytest

from qa import helpers


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    yield


class TestVoiceAbsent(unittest.TestCase):
    def test_talk_returns_503_voice_not_installed(self):
        # conftest blocks faster_whisper, so this is the no-voice install.
        r = CLIENT.post("/api/talk", files={
            "audio": ("clip.wav", io.BytesIO(b"RIFF0000WAVE"), "audio/wav")})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn("voice", str(r.json()).lower())


class TestLearningDisabled(unittest.TestCase):
    def test_learning_states_answer_cleanly(self):
        c = CLIENT
        for path in ("/api/learning/code/state", "/api/learning/chat/state"):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_learning_run_does_not_500(self):
        r = CLIENT.post("/api/learning/run")
        self.assertLess(r.status_code, 500, r.text)


class TestAgentAbsent(unittest.TestCase):
    def test_status_shows_toolless_floor_not_error(self):
        body = CLIENT.get("/api/hub/agent/status").json()
        self.assertFalse(body.get("tools"), body)   # tool-less floor, no 500

    def test_status_distinguishes_disabled_from_broken(self):
        # QA runs with AVA_AGENT_ENABLED=0, so the Hub must be able to say
        # "turned off" rather than the ambiguous "not ready".
        body = CLIENT.get("/api/hub/agent/status").json()
        self.assertIn("enabled", body)
        self.assertFalse(body["enabled"], body)

    def test_chat_still_works_on_the_floor(self):
        from qa.conftest import FAKE_LLM
        FAKE_LLM.reply = "Floor mode reply."
        r = CLIENT.post("/api/talk-text", data={"text": "hi from the floor"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Floor mode", r.json()["reply"])


class TestRouterAbsent(unittest.TestCase):
    def test_model_endpoints_degrade(self):
        c = CLIENT
        # No inference router is running in QA: the model pill/dropdown APIs
        # must answer (empty/null), not error.
        self.assertEqual(c.get("/api/model").status_code, 200)
        r = c.get("/api/code/models")
        self.assertLess(r.status_code, 500)


class TestFeatureFlags(unittest.TestCase):
    def test_hub_system_reports_feature_flags(self):
        body = CLIENT.get("/api/hub/system").json()
        for flag in ("voice", "web_search"):
            self.assertIn(flag, body)
