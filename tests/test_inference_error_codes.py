"""A failed turn must say what failed, and where to go.

`raise_for_status()` produced "404 Client Error: Not Found for url:
http://127.0.0.1:8010/v1/chat/completions" — the ROUTER's own loopback address.
That is neither where the problem is nor anywhere the owner can act, and it
discarded the one useful sentence: Ollama's body says `model 'llama3.2' not
found`. The chat then showed a raw URL with no next step.

The codes follow the shape features.preflight already emits, which is what
frontend/src/lib/fixes.ts pattern-matches: `<key>_down` routes to Operations
with no frontend change. `model_unknown` is the single new pattern, because when
the engine is up and merely lacks the model, Operations is the wrong place to
send someone — nothing is down to restart.
"""
from __future__ import annotations

import importlib
import pathlib

direct = importlib.import_module("ava_bridge.runtime.direct")
ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Resp:
    def __init__(self, body, status=404, text=""):
        self._body, self.status_code, self.text = body, status, text
        self.ok = status < 400

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_a_missing_model_is_named_as_such() -> None:
    e = direct._inference_error(_Resp({"error": "model 'llama3.2' not found"}))
    assert e.code == "model_unknown"
    assert "llama3.2" in str(e), (
        "the engine named the model it lacks and we dropped it — that string is "
        "the whole diagnosis")


def test_the_openai_error_envelope_is_understood_too() -> None:
    e = direct._inference_error(_Resp(
        {"error": {"message": "The model `gpt-9` does not exist", "code": "model_not_found"}}))
    assert e.code == "model_unknown"
    assert "gpt-9" in str(e)


def test_anything_else_is_a_down_engine() -> None:
    """Defaulting to model_unknown would send someone to pick a model when the
    real problem is that nothing is running."""
    e = direct._inference_error(_Resp(None, status=502, text="bad gateway"))
    assert e.code == "inference_down"
    assert "bad gateway" in str(e)


def test_the_routers_own_url_is_not_what_the_user_is_shown() -> None:
    e = direct._inference_error(_Resp({"error": "model 'x' not found"}))
    assert "127.0.0.1" not in str(e) and "http" not in str(e).lower(), (
        "the failure message names a loopback URL again, which is the defect: "
        "it points at the hop rather than the engine")


def test_an_empty_body_still_yields_something_actionable() -> None:
    e = direct._inference_error(_Resp(None, status=503, text=""))
    assert "503" in str(e)
    assert e.code == "inference_down"


def test_the_turn_carries_the_code_to_the_ui() -> None:
    """/api/turn/<id> returns the turn dict verbatim, so putting the code in the
    turn state is the entire transport. If this stops happening the fix-it link
    silently disappears and the chat is back to a bare string."""
    src = (ROOT / "ava_bridge" / "turns.py").read_text(encoding="utf-8")
    assert src.count('error_code=getattr(e, "code", "")') >= 2, (
        "a turn error handler stopped forwarding error_code — that path shows a "
        "message with no way forward")


def test_the_frontend_routes_model_unknown_somewhere_useful() -> None:
    """Operations is where `_down` goes and it is wrong for this: the engine is
    up, so there is nothing there to restart."""
    fixes = (ROOT / "frontend" / "src" / "lib" / "fixes.ts").read_text(encoding="utf-8")
    assert "model_unknown" in fixes
    i = fixes.index("model_unknown")
    assert "hub/agent" in fixes[i:i + 400], (
        "model_unknown no longer routes to Setup → Agent")
