"""Something must answer "can Ava actually reply?" before the owner asks.

Nothing did. setup_completed() reports a flag and says so in its own docstring —
it stays True for an install whose model was later deleted, whose engine stopped,
or whose config was hand-edited. So the first thing that ever noticed was the
engine, one turn later, and a first-time owner read "Failed" as "this product
does not work".

The `code` field is the contract: it feeds frontend/src/lib/fixes.ts, which
resolves a destination from the code PATTERN, so this route needs no mapping
table of its own.
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava_bridge import hub_api, models, router_app, runtime


def _backend(model="llama3.2", engine="ollama"):
    return {"id": "b", "url": "http://ollama:11434/v1", "model": model,
            "label": "b", "engine": engine, "api_key": None,
            "tools": "native", "fit": None}


@pytest.fixture
def client(monkeypatch):
    """The route with the agent runtime pinned OFF.

    The route now names its model via models.effective_brain(), whose FIRST
    branch is the agent sandbox. Left real, every case below would diagnose
    whichever sandbox the developer's own box happens to have onboarded, and
    diagnose the mocked config on CI — the same test asserting two different
    things depending on the machine. The sandbox branch gets its own case at
    the bottom instead.
    """
    monkeypatch.setattr(runtime, "active", runtime.direct)
    app = FastAPI()
    app.include_router(hub_api.router)
    return TestClient(app)


def test_no_model_configured_is_reported_as_a_missing_model(client) -> None:
    """Distinct from "engine down" on purpose: nothing is broken to restart, a
    value is simply absent, and Operations is the wrong place to send someone."""
    with mock.patch.object(router_app, "load_backends", lambda: [_backend(model="")]):
        r = client.get("/api/hub/agent/inference").json()
    assert r["ok"] is False
    assert r["code"] == "model_unknown", (
        "a missing model reported as anything else routes the owner to the "
        "wrong page")


def test_an_unreachable_engine_is_not_called_a_missing_model(client) -> None:
    """On a gpu install vLLM can spend half an hour pulling weights. Telling that
    owner their model is missing sends them to fix something that is fine."""
    with mock.patch.object(router_app, "load_backends", lambda: [_backend()]), \
         mock.patch.object(models, "probe_serving", lambda *a, **k: (False, [])):
        r = client.get("/api/hub/agent/inference").json()
    assert r["ok"] is False
    assert r["code"] == "inference_down"


def test_a_reachable_engine_without_the_model_is_a_missing_model(client) -> None:
    with mock.patch.object(router_app, "load_backends", lambda: [_backend()]), \
         mock.patch.object(models, "probe_serving", lambda *a, **k: (True, ["qwen:7b"])):
        r = client.get("/api/hub/agent/inference").json()
    assert r["ok"] is False
    assert r["code"] == "model_unknown"
    assert "llama3.2" in r["detail"], "the diagnosis does not name the model"


def test_a_working_install_reports_ok_and_no_code(client) -> None:
    """An ok with a code set would render a banner over a healthy install."""
    with mock.patch.object(router_app, "load_backends", lambda: [_backend()]), \
         mock.patch.object(models, "probe_serving",
                           lambda *a, **k: (True, ["llama3.2:latest"])):
        r = client.get("/api/hub/agent/inference").json()
    assert r["ok"] is True
    assert not r["code"]


def test_the_bare_ollama_tag_is_not_reported_missing(client) -> None:
    """install.sh writes `llama3.2`; Ollama reports `llama3.2:latest`. Calling
    that missing would put a permanent banner on every correct cpu install."""
    with mock.patch.object(router_app, "load_backends", lambda: [_backend()]), \
         mock.patch.object(models, "probe_serving",
                           lambda *a, **k: (True, ["llama3.2:latest"])):
        assert client.get("/api/hub/agent/inference").json()["ok"] is True


def test_a_broken_config_answers_instead_of_raising(client) -> None:
    """The health check is the least important thing on the page; it must never
    be what takes the shell down.

    load_backends() is still called for exactly this: models.effective_brain()
    deliberately swallows a broken config (a monitor degrades, it does not
    raise), so it can only report "no model" — and "unreadable yaml" sends the
    owner somewhere else entirely.
    """
    def boom():
        raise ValueError("bad yaml")
    with mock.patch.object(router_app, "load_backends", boom):
        r = client.get("/api/hub/agent/inference")
    assert r.status_code == 200
    assert r.json()["code"] == "config_unparseable"


def test_the_model_diagnosed_is_the_brain_not_the_first_one_listed(client) -> None:
    """The route reported on `the first backend that has a model set`, which is
    the brain only by coincidence. With roles.chat pointing at the second one,
    it graded an engine no turn ever reaches — a green badge for a model nobody
    talks to, or a red one naming a model that is not the problem."""
    listed = [_backend(model="unused-fast-model"),
              dict(_backend(model="the-brain"), id="brain")]
    with mock.patch.object(router_app, "load_backends", lambda: listed), \
         mock.patch.object(models.settings, "get",
                           lambda k, d=None: {"roles": {"chat": "brain"}}
                           if k == "inference" else d), \
         mock.patch.object(models, "probe_serving", lambda *a, **k: (True, ["the-brain"])):
        r = client.get("/api/hub/agent/inference").json()
    assert r["ok"] is True
    assert r["model"] == "the-brain", (
        "Setup is grading a different model than the one that answers turns")


def test_a_live_agent_sandbox_is_reported_ok_and_not_probed(client) -> None:
    """The agent runtime answers turns inside its sandbox and owns the model
    endpoint, so the resolver hands us no base URL. Probing the empty one would
    read `inference_down` and paint a red banner over an install that is
    replying perfectly well."""
    probed = []
    agent_brain = {"source": "agent", "backend_id": "", "engine": "ollama",
                   "model_id": "nvidia/Reasoner-30B", "label": "Reasoner-30B",
                   "base_url": "", "api_key": "", "local": True, "implicit": False}
    with mock.patch.object(models, "effective_brain", lambda: dict(agent_brain)), \
         mock.patch.object(models, "probe_serving",
                           lambda *a, **k: (probed.append(a), (False, []))[1]):
        r = client.get("/api/hub/agent/inference").json()
    assert r["ok"] is True and not r["code"]
    assert r["model"] == "nvidia/Reasoner-30B"
    assert not probed, "there is no endpoint here to probe; the sandbox holds it"
