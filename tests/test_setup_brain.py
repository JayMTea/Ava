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


@pytest.fixture(autouse=True)
def _no_agent_by_default(monkeypatch):
    """These tests are about the INFERENCE-BACKEND path.

    `recommend_brain` now asks `models.effective_brain()` first, because a live
    agent sandbox owns the model endpoint and the backends below are then never
    consulted. That makes the answer depend on whether the machine running the
    tests happens to have a sandbox — a dependency these tests already had
    through `runtime`, and which nothing pinned. Neutralised here so the file is
    hermetic; the tests that are ABOUT the agent case patch it themselves.
    """
    from ava_bridge import models
    monkeypatch.setattr(models, "effective_brain",
                        lambda: {"source": "configured", "model_id": ""})


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
           "inference.backends": {"local": {"engine": "vllm", "model": "mistralai/Mistral-7B-Instruct-v0.3",
                                            "base_url": "http://vllm:8002/v1"}}}
    monkeypatch.setattr(setup_wizard.settings, "get",
                        lambda key, default=None: cfg.get(key, default))
    b = setup_wizard.recommend_brain()
    assert b["source"] == "configured"
    assert b["model"] == "mistralai/Mistral-7B-Instruct-v0.3"
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


def test_a_live_agent_sandbox_owns_the_brain_and_the_wizard_says_so(monkeypatch) -> None:
    """When the agent runtime is live it OWNS the model endpoint, and turns never
    reach the inference backends this screen reads.

    `recommend_brain` was unaware of it entirely, so first run confirmed a
    backend that would not serve one message: the owner picks a model, the box
    uses a different one, and the two screens that report the brain disagree
    forever. Asked through `models.effective_brain()` — the ONE resolver — so
    this cannot become a fourth answer to the same question.
    """
    from ava_bridge import models, setup_wizard

    monkeypatch.setattr(models, "effective_brain", lambda: {
        "source": "agent", "model_id": "nvidia/Reasoner-30B",
        "label": "Reasoner-30B", "engine": "ollama", "backend_id": "",
        "base_url": "", "local": True, "implicit": False})

    out = setup_wizard.recommend_brain()

    assert out["source"] == "agent"
    assert out["agent_owns_it"] is True
    assert out["model"] == "nvidia/Reasoner-30B"
    assert out["writes_config"] is False, (
        "the sandbox holds the endpoint; the wizard has nothing to write")
    assert "nemoclaw onboard" in out["consequence"]


def test_without_an_agent_the_wizard_reads_the_backends_as_before(monkeypatch) -> None:
    """The short-circuit must not swallow the ordinary path."""
    from ava_bridge import models, setup_wizard

    monkeypatch.setattr(models, "effective_brain",
                        lambda: {"source": "configured", "model_id": "m"})
    out = setup_wizard.recommend_brain()
    assert out["agent_owns_it"] is False
    assert out["source"] in ("none", "configured", "installed")


def test_a_broken_resolver_does_not_break_the_wizard(monkeypatch) -> None:
    from ava_bridge import models, setup_wizard

    def _boom():
        raise RuntimeError("no runtime")

    monkeypatch.setattr(models, "effective_brain", _boom)
    out = setup_wizard.recommend_brain()
    assert out["agent_owns_it"] is False


def test_an_agent_whose_model_cannot_be_read_says_so_rather_than_guessing(monkeypatch) -> None:
    """The sandbox exists but its container is stopped, so nothing could read
    what it holds. Naming a backend here would be worse: it will not serve a
    single turn either way, and claiming a model we did not read is the exact
    inference the brain-visibility rule forbids."""
    from ava_bridge import models, setup_wizard

    monkeypatch.setattr(models, "effective_brain",
                        lambda: {"source": "agent", "model_id": "",
                                 "engine": "nemoclaw"})
    out = setup_wizard.recommend_brain()

    assert out["agent_owns_it"] is True
    assert out["model"] == ""
    assert out["live"] is False, "claimed a live brain it could not read"
    assert "not running" in out["consequence"]
