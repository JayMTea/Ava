"""First run confirms the model already in force; it does not ask for its id.

The wizard demanded a typed model id and 400'd on blank, while the answer sat in
the bridge's own environment: install.sh resolves and pulls a model, writes
AVA_MODEL to deploy/.env, and docker-compose.yml passes it in as
AVA_BACKEND_MODEL. `_candidates()` read AVA_BACKEND_URL and AVA_BACKEND_ENGINE
and stopped one variable short of it.

The load-bearing property is NOT "prefill the box" — that would be worse. On the
gpu profile nothing is answering when the wizard runs (no depends_on gates ava on
vllm), so the prefill guard leaves engine and base URL at their Ollama defaults;
filling in the model completes a form with two of three fields wrong. And
router_app.load_backends() builds the env backend ONLY while inference.backends
is empty, so writing the value — even correctly — permanently shadows the
installer's backend and stops it tracking deploy/.env.

So confirming must write NOTHING to inference. That is what these pin.
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava_bridge import setup_wizard


@pytest.fixture
def env_install(monkeypatch):
    """A cpu Docker install: compose has passed the installer's model in."""
    monkeypatch.setenv("AVA_BACKEND_URL", "http://ollama:11434/v1")
    monkeypatch.setenv("AVA_BACKEND_ENGINE", "ollama")
    monkeypatch.setenv("AVA_BACKEND_MODEL", "llama3.2")
    monkeypatch.setattr(setup_wizard, "_probe", lambda *a, **k: True)
    monkeypatch.setattr(setup_wizard.settings, "get", lambda *a, **k: None)


def test_the_installers_model_is_found_instead_of_asked_for(env_install) -> None:
    b = setup_wizard.recommend_brain()
    assert b["model"] == "llama3.2", (
        "recommend_brain() did not see AVA_BACKEND_MODEL, so first run is back to "
        "asking the user to type a value the machine is already holding")
    assert b["source"] == "installed"
    assert b["engine"] == "ollama"


def test_confirming_the_installers_model_writes_no_inference_block(env_install) -> None:
    """The whole point. router_app.load_backends() serves the env backend only
    while inference.backends is empty, so persisting this value would shadow the
    thing that is working and freeze it against deploy/.env."""
    b = setup_wizard.recommend_brain()
    assert b["writes_config"] is False, (
        "the wizard would copy the installer's model into ava.yaml, permanently "
        "shadowing the env backend it came from")


def test_a_configured_model_wins_over_the_environment(monkeypatch) -> None:
    """A model the owner chose is already written and already in force; the
    installer's env value must not override what they picked."""
    monkeypatch.setenv("AVA_BACKEND_MODEL", "llama3.2")
    monkeypatch.setenv("AVA_BACKEND_URL", "http://ollama:11434/v1")
    monkeypatch.setattr(setup_wizard, "_probe", lambda *a, **k: False)
    cfg = {"inference.primary": "local",
           "inference.backends": {"local": {"engine": "vllm", "model": "Qwen/Qwen2.5-7B-Instruct",
                                            "base_url": "http://vllm:8002/v1"}}}
    monkeypatch.setattr(setup_wizard.settings, "get",
                        lambda key, default=None: cfg.get(key, default))
    b = setup_wizard.recommend_brain()
    assert b["source"] == "configured"
    assert b["model"] == "Qwen/Qwen2.5-7B-Instruct"
    assert b["writes_config"] is False


def test_nothing_anywhere_is_reported_as_nothing(monkeypatch) -> None:
    """No brain means there IS a real choice to make, and the wizard must show
    the chooser rather than a card confirming something that does not exist."""
    for k in ("AVA_BACKEND_URL", "AVA_BACKEND_ENGINE", "AVA_BACKEND_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(setup_wizard.settings, "get", lambda *a, **k: None)
    b = setup_wizard.recommend_brain()
    assert b["source"] == "none"
    assert b["model"] == ""


def test_the_backends_probe_stops_discarding_the_model(env_install) -> None:
    """_candidates() read this source's siblings and dropped the model on the
    floor, which is the root of the whole defect."""
    env = [c for c in setup_wizard._candidates() if c["id"] == "env"]
    assert env and env[0]["model"] == "llama3.2", (
        "the env candidate carries no model, so the wizard is again holding the "
        "answer and not using it")


# ---- the save verb ---------------------------------------------------------
def _client() -> TestClient:
    app = FastAPI()
    app.include_router(setup_wizard.router)
    return TestClient(app)


def test_confirming_completes_setup_and_leaves_inference_alone(env_install) -> None:
    saved: dict = {}

    def _capture(patch):
        saved.update(patch)

    with mock.patch.object(setup_wizard.settings, "save_patch", _capture):
        r = _client().post("/api/setup/save",
                           json={"inference": {"mode": "installed"}, "features": {}})
    assert r.status_code == 200, r.text
    assert saved.get("setup", {}).get("completed") is True
    assert "inference" not in saved, (
        f"confirming wrote an inference block ({saved.get('inference')!r}) — that "
        "shadows the env backend it just confirmed")


def test_confirming_nothing_is_refused(monkeypatch) -> None:
    """The client asserting "there is a brain" must not be able to complete setup
    against nothing, so the server re-derives it."""
    for k in ("AVA_BACKEND_URL", "AVA_BACKEND_ENGINE", "AVA_BACKEND_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(setup_wizard.settings, "get", lambda *a, **k: None)
    with mock.patch.object(setup_wizard.settings, "save_patch", lambda p: None):
        r = _client().post("/api/setup/save",
                           json={"inference": {"mode": "installed"}, "features": {}})
    assert r.status_code == 400
    assert r.json().get("field") == "model"


def test_choosing_a_model_explicitly_still_writes_it(env_install) -> None:
    """The escape hatch is untouched: someone who picks a model gets it saved."""
    saved: dict = {}
    with mock.patch.object(setup_wizard.settings, "save_patch", saved.update):
        r = _client().post("/api/setup/save", json={
            "inference": {"mode": "local", "engine": "ollama",
                          "base_url": "http://127.0.0.1:11434/v1", "model": "mistral"},
            "features": {}})
    assert r.status_code == 200, r.text
    assert saved["inference"]["backends"]["local"]["model"] == "mistral"


def test_a_blank_model_is_still_refused(env_install) -> None:
    """The 400 that stops a half-configured install must survive this change."""
    with mock.patch.object(setup_wizard.settings, "save_patch", lambda p: None):
        r = _client().post("/api/setup/save", json={
            "inference": {"mode": "local", "engine": "ollama",
                          "base_url": "http://127.0.0.1:11434/v1", "model": ""},
            "features": {}})
    assert r.status_code == 400
    assert r.json().get("field") == "model"
