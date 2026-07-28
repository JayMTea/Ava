"""Whole-app QA suite bootstrap.

CRITICAL ORDERING: ava_bridge.settings freezes AVA_HOME (and loads .env with
setdefault) the first time it is imported, so this file configures the process
environment at IMPORT TIME, before pytest collects a single test. That is also
why this suite must run in its own pytest session (`pytest qa` / qa/run.sh) and
must never be mixed into `pytest tests/`.

The suite gets:
  * a throwaway AVA_HOME (a genuine first-run home — no password, no config),
  * a fake OpenAI-compatible LLM serving DirectRuntime,
  * a fake the GPU service serving media jobs,
  * a fake "user's app" to connect as a connector,
  * the real `phone_bridge:app` under FastAPI TestClient with real lifespan.
"""
import os
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from qa.env_recipe import free_port, hermetic_env  # noqa: E402

QA_HOME = tempfile.mkdtemp(prefix="ava-qa-home-")
_LLM_PORT = free_port()
_gpusvc_PORT = free_port()
_APP_PORT = free_port()

os.environ.update(hermetic_env(
    QA_HOME,
    llm_url=f"http://127.0.0.1:{_LLM_PORT}/v1/chat/completions",
    gpusvc_url=f"http://127.0.0.1:{_gpusvc_PORT}",
    # An unused port: with the default :8010, a production router on the SAME
    # machine would answer this suite's /api/model probes — isolation leak.
    router_port=free_port()))

# Hermetic voice-less path: phone_bridge._ensure_loaded treats an ImportError of
# faster_whisper as "voice extras not installed". Blocking the module keeps
# startup fast and makes the /api/talk 503 contract deterministic even on this
# box, where the real voice stack exists.
sys.modules["faster_whisper"] = None

from qa.fakes.fake_app import FakeApp        # noqa: E402
from qa.fakes.fake_gpusvc import Fakegpusvc    # noqa: E402
from qa.fakes.fake_llm import FakeLLM        # noqa: E402

FAKE_LLM = FakeLLM(_LLM_PORT).start()
FAKE_gpusvc = Fakegpusvc(_gpusvc_PORT).start()
FAKE_APP = FakeApp(_APP_PORT).start()

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def bridge():
    """The real phone_bridge:app under TestClient, lifespan (startup hooks) run.
    Session-scoped: import is heavy and AVA_HOME is frozen anyway."""
    from fastapi.testclient import TestClient
    import phone_bridge
    # client=: TestClient reports `request.client.host` as the literal string
    # "testclient", which is not an IP address. auth._is_loopback fails closed on
    # anything it cannot parse (a gate that treated "unparseable" as "local"
    # would be open to anything that confused it), so the suite would 403 at
    # /setup. Presenting a real loopback address is what the product sees from a
    # browser on the same machine.
    with TestClient(phone_bridge.app, client=("127.0.0.1", 50000)) as client:
        yield client


@pytest.fixture()
def authed(bridge):
    """The bridge client, guaranteed logged in (idempotent — creates the QA
    password via the real /setup flow if this is still a pristine home)."""
    from qa import helpers
    helpers.ensure_authed(bridge)
    return bridge


@pytest.fixture()
def fake_llm():
    FAKE_LLM.reset()
    yield FAKE_LLM
    FAKE_LLM.reset()


@pytest.fixture()
def fake_gpusvc():
    FAKE_gpusvc.reset()
    yield FAKE_gpusvc
    FAKE_gpusvc.reset()


@pytest.fixture()
def fake_app():
    FAKE_APP.reset()
    yield FAKE_APP
    FAKE_APP.reset()
