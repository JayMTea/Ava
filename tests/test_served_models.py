"""One parser for "what is this engine actually serving".

First-run setup asked the user to TYPE a model id and sent it verbatim, so a
near-miss produced a config that looked right and failed on the first message.
Reading the engine's own list is how that question stops being asked — but only
if the list is read from the right path and compared honestly.

Two shapes exist and both are load-bearing: OpenAI-compatible engines answer
`{"data":[{"id":...}]}` at /models, Ollama answers `{"models":[{"name":...}]}` at
/api/tags off the ROOT rather than under /v1. hardware.py hardcoded the first
pair, so an Ollama backend worked only through its compatibility layer and
llama.cpp — whose health is /health but whose models are /models — was asked at
whichever path happened to be configured.
"""
from __future__ import annotations

from unittest import mock

from ava_bridge import models


class _Resp:
    def __init__(self, body, ok=True):
        self.ok, self._body = ok, body

    def json(self):
        return self._body


def _get(body, ok=True):
    seen = {}

    def fake(url, timeout=0, headers=None):
        seen["url"] = url
        return _Resp(body, ok)
    return fake, seen


def test_ollama_is_asked_off_the_root_not_under_v1() -> None:
    """/api/tags does not exist under /v1. Getting this wrong returns 404 and
    the model list silently reads empty, which downstream means "your model is
    missing" — a false accusation about a working install."""
    assert models.models_url("http://ollama:11434/v1", "ollama") == \
        "http://ollama:11434/api/tags"


def test_llamacpp_models_path_is_not_its_health_path() -> None:
    """Its health is /health; asking there for models returns no list at all."""
    assert models.models_url("http://127.0.0.1:8080/v1", "llamacpp") == \
        "http://127.0.0.1:8080/v1/models"


def test_the_openai_envelope_is_parsed() -> None:
    # SORTED, deduped — `_fetch_served` returns `sorted(dict.fromkeys(out))`, so
    # "b" precedes "mistralai/…". This expectation used to read in the order the
    # payload declared and passed only because the id started with a capital,
    # which sorts before lowercase in ASCII. Nothing was testing the sort.
    fake, seen = _get({"data": [{"id": "mistralai/Mistral-7B-Instruct-v0.3"}, {"id": "b"}]})
    with mock.patch("requests.get", fake):
        assert models.served_models("http://vllm:8002/v1", "vllm") == \
            ["b", "mistralai/Mistral-7B-Instruct-v0.3"]
    assert seen["url"].endswith("/v1/models")


def test_the_ollama_envelope_is_parsed() -> None:
    fake, _ = _get({"models": [{"name": "llama3.2:latest"}, {"name": "gemma2:9b"}]})
    with mock.patch("requests.get", fake):
        assert models.served_models("http://ollama:11434/v1", "ollama") == \
            ["gemma2:9b", "llama3.2:latest"]   # sorted, like the OpenAI case


def test_down_and_empty_are_different_answers() -> None:
    """An engine that is up with an empty store is reachable and serving
    nothing. Collapsing that into "down" reports a fine service as an outage."""
    fake, _ = _get({"models": []})
    with mock.patch("requests.get", fake):
        assert models.probe_serving("http://ollama:11434/v1", "ollama") == (True, [])

    def boom(*a, **k):
        raise OSError("connection refused")
    with mock.patch("requests.get", boom):
        assert models.probe_serving("http://ollama:11434/v1", "ollama") == (False, [])


def test_a_bare_ollama_tag_matches_its_latest() -> None:
    """`ollama pull llama3.2` stores `llama3.2:latest`, and that bare name is
    what install.sh writes into AVA_MODEL. Refusing to match them would tell
    every cpu installer their model was missing."""
    assert models.match_served("llama3.2", ["llama3.2:latest"]) == "llama3.2:latest"


def test_matching_is_otherwise_exact() -> None:
    """Ava sends the id verbatim, so a near-miss is a first-message failure, not
    a helpful guess. `llama3` is not `llama3.2`."""
    assert models.match_served("llama3", ["llama3.2:latest"]) == ""
    assert models.match_served("some-7b-instruct",
                               ["mistralai/Mistral-7B-Instruct-v0.3"]) == ""
    assert models.match_served("mistralai/Mistral-7B-Instruct-v0.3",
                               ["mistralai/Mistral-7B-Instruct-v0.3"]) == \
        "mistralai/Mistral-7B-Instruct-v0.3"


def test_a_tagged_request_is_never_widened() -> None:
    """Asking for :7b must not be satisfied by :70b."""
    assert models.match_served("llama3.1:7b", ["llama3.1:70b"]) == ""


# ── Ollama presence is an EXACT tag match ────────────────────────────────────
# `tag.split(":")[0] in out.stdout` answered yes for `llama3.1:70b` when only
# `llama3.1:8b` was pulled — the repo name matches and the size does not. The
# required model then read as present, the pull was skipped, and the first chat
# turn failed against a model the engine does not hold.

_OLLAMA_LIST = """NAME                ID              SIZE      MODIFIED
llama3.1:8b         abc123          4.7 GB    2 days ago
gemma2-coder:9b     def456          4.4 GB    1 week ago
"""


def _present(tag, stdout=_OLLAMA_LIST, monkeypatch=None):
    from unittest import mock

    from ava_bridge import models

    class _CP:
        pass

    cp = _CP()
    cp.stdout = stdout
    with mock.patch.object(models.shutil, "which", lambda _n: "/usr/bin/ollama"), \
         mock.patch.object(models.subprocess, "run", lambda *a, **k: cp):
        return models.ollama_present(tag, "/tmp/ollama")


def test_a_different_size_of_the_same_repo_is_not_present() -> None:
    assert _present("llama3.1:8b") is True
    assert _present("llama3.1:70b") is False, (
        "a 70B was reported present because an 8B of the same repo was pulled")


def test_an_untagged_request_means_latest() -> None:
    assert _present("llama3.1") is False, "llama3.1:latest is not pulled"
    assert _present("mistral", stdout="NAME  ID\nmistral:latest  x\n") is True


def test_a_name_appearing_elsewhere_on_the_line_does_not_match() -> None:
    assert _present("abc123") is False, "matched an ID column, not a NAME"


def test_nothing_pulled_is_not_present() -> None:
    assert _present("llama3.1:8b", stdout="NAME  ID  SIZE  MODIFIED\n") is False
