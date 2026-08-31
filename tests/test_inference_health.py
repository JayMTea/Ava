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
         mock.patch.object(models, "probe_serving", lambda *a, **k: (True, ["gemma2:9b"])):
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


def test_a_stopped_agent_sandbox_is_not_reported_healthy(client) -> None:
    """"The resolver picked the sandbox" is not the same claim as "the sandbox is
    up".

    `runtime.active()` gates on `available()`, a 30s-cached check that the
    sandbox EXISTS in `nemoclaw list` — and a stopped container still exists. So
    this branch answered a flat `ok: True` for an agent that could not reply, and
    the owner got a green banner over a dead assistant. `live()` is the
    observation; liveness is never inferred from configuration.
    """
    from ava_bridge import runtime

    agent_brain = {"source": "agent", "backend_id": "", "engine": "ollama",
                   "model_id": "nvidia/Reasoner-30B", "label": "Reasoner-30B",
                   "base_url": "", "api_key": "", "local": True, "implicit": False}

    class _Stopped:
        name = "nemoclaw"

        def live(self):
            return {"live": False,
                    "reason": "the sandbox container for 'my-assistant' is not running"}

    with mock.patch.object(models, "effective_brain", lambda: dict(agent_brain)), \
         mock.patch.object(runtime, "active", lambda: _Stopped()):
        r = client.get("/api/hub/agent/inference").json()

    assert r["ok"] is False
    assert r["code"] == "agent_down"
    assert "not running" in r["detail"]


def test_a_runtime_that_cannot_be_probed_is_not_reported_healthy(client) -> None:
    """A probe that raises is an unknown, and an unknown must not read as a yes."""
    from ava_bridge import runtime

    agent_brain = {"source": "agent", "backend_id": "", "engine": "", "model_id": "m",
                   "label": "m", "base_url": "", "api_key": "", "local": True,
                   "implicit": False}

    class _Broken:
        name = "nemoclaw"

        def live(self):
            raise OSError("docker socket gone")

    with mock.patch.object(models, "effective_brain", lambda: dict(agent_brain)), \
         mock.patch.object(runtime, "active", lambda: _Broken()):
        r = client.get("/api/hub/agent/inference").json()
    assert r["ok"] is False and r["code"] == "agent_down"


# --------------------------------------------------------------------------- #
# The runtime owns the brain and could not NAME it.
#
# A distinct fault from "nothing is configured", reached through the same empty
# model id, and the generic sentence is wrong advice for it: the model was
# chosen by `nemoclaw onboard`, so Setup is not where it gets fixed. Observed
# for real — the agent shim's /healthz sent no `model`, so sandbox_info()
# answered None and every surface called a reporting fault a config one.
# --------------------------------------------------------------------------- #
def _unnamed_agent_brain() -> dict:
    """What the resolver returns when the runtime owns the brain but cannot name
    it: source is `agent`, and the id is empty."""
    return {"source": "agent", "backend_id": "", "engine": "compatible-endpoint",
            "model_id": "", "label": "", "base_url": "", "api_key": "",
            "local": False, "implicit": False}


class _LiveAgent:
    """A runtime that is observably answering, with a chosen capability set."""

    name = "remote"

    def __init__(self, caps, local=False):
        self._caps, self._local = caps, local

    def live(self):
        return {"live": True, "reason": ""}

    def capabilities(self):
        return list(self._caps)

    def is_local(self):
        return self._local


def test_a_runtime_that_reports_no_model_is_not_called_unconfigured(client) -> None:
    """It advertises `health.model`, so it LOOKED — the sandbox genuinely has
    none onboarded. Sending this owner to Setup asks them to change a setting
    that is already correct."""
    from ava_bridge import runtime

    with mock.patch.object(models, "effective_brain", _unnamed_agent_brain), \
         mock.patch.object(runtime, "active", lambda: _LiveAgent(["health.model"])):
        r = client.get("/api/hub/agent/inference").json()
    assert r["ok"] is False
    assert r["code"] == "model_unknown", "fixes.ts routes on this pattern"
    assert "onboard" in r["detail"], (
        "the fix is `nemoclaw onboard` on the runtime's host, and the diagnosis "
        "has to say so")
    assert "No model is configured" not in r["detail"], (
        "the generic sentence blames the config for a runtime's silence")


def test_an_agent_too_old_to_answer_is_told_to_rebuild(client) -> None:
    """No `health.model` in its capabilities means the container predates the
    field and cannot answer at all — a different machine and a different fix
    from the case above, which is the entire reason the flag exists."""
    from ava_bridge import runtime

    with mock.patch.object(models, "effective_brain", _unnamed_agent_brain), \
         mock.patch.object(runtime, "active", lambda: _LiveAgent([], local=False)):
        r = client.get("/api/hub/agent/inference").json()
    assert r["code"] == "model_unknown"
    assert "rebuild" in r["detail"], (
        "an agent container that cannot report its model needs rebuilding, and "
        "nothing else the owner could do here will help")


def test_a_stopped_agent_with_no_model_is_reported_down_not_unnamed(client) -> None:
    """ORDERING IS THE DIAGNOSIS. An unreachable shim advertises no
    capabilities, and an empty capability list is indistinguishable from "too
    old to advertise one" — so an unguarded check tells the owner of a STOPPED
    container to rebuild it. `agent_down`, carrying the probe's own reason, is
    the answer that names what they have to fix.
    """
    from ava_bridge import runtime

    class _Stopped:
        name = "remote"

        def live(self):
            return {"live": False, "reason": "could not reach http://agent:9100"}

        def capabilities(self):
            return []          # nobody answered, so nothing was advertised

        def is_local(self):
            return False

    with mock.patch.object(models, "effective_brain", _unnamed_agent_brain), \
         mock.patch.object(runtime, "active", lambda: _Stopped()):
        r = client.get("/api/hub/agent/inference").json()
    assert r["code"] == "agent_down", (
        "a stopped agent was diagnosed as one that cannot name its model — true "
        "of a stopped container, and useless to whoever has to restart it")
    assert "agent:9100" in r["detail"], (
        "the probe's own reason is the useful half — it names what to restart")
