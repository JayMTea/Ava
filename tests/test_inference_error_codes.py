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


def test_a_responder_that_names_its_own_failure_is_believed() -> None:
    """`error_code` on the body outranks sniffing the prose.

    Ava's OWN router sets one. This function only ever read the message text,
    so the router's "No model is configured, so there is nothing to answer
    with" matched none of the missing-model hints, fell through to
    `inference_down`, and the chat UI offered "Check it on Operations" — a
    service health page, on an install where nothing was down and nothing had
    ever been configured. It was unreachable while the router still invented a
    default backend; removing that default made it the FIRST thing a new owner
    would hit.
    """
    e = direct._inference_error(_Resp(
        {"error": "No model is configured, so there is nothing to answer with. "
                  "Choose one in Setup -> Agent.",
         "error_code": "model_unknown"}, status=400))
    assert e.code == "model_unknown", (
        "the router named the failure; prose-sniffing must not overrule it")


def test_prose_sniffing_still_covers_engines_that_name_nothing() -> None:
    """A third-party engine sets no `error_code`, so the hints still apply."""
    e = direct._inference_error(_Resp({"error": "model 'llama3.2' not found"}))
    assert e.code == "model_unknown"


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
    """The transport is FOUR links, and this test used to check one.

    It asserted that turns.py contains the forwarding line and called that "the
    entire transport" — which was wrong, and it passed for the whole time the
    feature was broken. The backend sent the code, /api/turn/<id> returned it,
    and the SPA dropped it: TurnStatus did not declare the field, the `cot` chat
    item had nowhere to keep it, and ChainOfThought rendered a bare
    `Failed: <text>`. A real first-run failure reached a user with no link.

    So check every link, including the three on the far side of the wire. A
    static scan is the only option here — the SPA has no component-render
    harness — but scanning BOTH sides is the difference between proving the
    wire is connected and proving one end of it exists.
    """
    src = (ROOT / "ava_bridge" / "turns.py").read_text(encoding="utf-8")
    assert src.count('error_code=getattr(e, "code", "")') >= 2, (
        "a turn error handler stopped forwarding error_code — that path shows a "
        "message with no way forward")

    fe = ROOT / "frontend" / "src"
    types_ = (fe / "lib" / "types.ts").read_text(encoding="utf-8")
    turn_block = types_[types_.index("interface TurnStatus"):]
    turn_block = turn_block[:turn_block.index("}")]
    assert "error_code" in turn_block, (
        "TurnStatus does not declare error_code, so TypeScript discards the "
        "field the backend sends and the failure has no link")

    items = (fe / "lib" / "chatItems.ts").read_text(encoding="utf-8")
    cot = items[items.index("kind: 'cot'"):]
    assert "code?" in cot[:cot.index("}")], (
        "the cot chat item cannot carry a code, so the poll has nowhere to put "
        "it even if it reads it")

    # DISCOVERED, not pinned to one filename. This assertion named
    # `hooks/useChat.ts`, and when the poll loop was extracted into
    # `hooks/chatDirect.ts` the guard failed — which is the good outcome, but
    # only because the string vanished entirely. Had it moved to a file this
    # still read, the check would have quietly started proving nothing. The
    # sibling guard `tests/test_no_blocking_routes.py` documents this exact
    # failure mode: "a guard with a fixed file list does not fail when code
    # moves away from it — it just stops checking."
    hooks = sorted((fe / "hooks").glob("*.ts"))
    assert hooks, "frontend/src/hooks holds no modules — has the layout moved?"
    # 2026-08-23: the finished-turn mapping moved AGAIN, out of the hooks into
    # the shared `applyTurnRecord` (lib/chatEvents.ts), so the polled and the
    # streamed strategy apply ONE record->items mapping instead of drifting
    # copies — the carrier there reads `code: turn.error_code`. Keep the
    # discovery across both layers rather than pinning the new home, for the
    # reason the paragraph above records.
    modules = hooks + sorted((fe / "lib").glob("*.ts"))
    carriers = [f.name for f in modules
                if not f.name.endswith(".test.ts")
                and ("code: s.error_code" in f.read_text(encoding="utf-8")
                     or "code: turn.error_code" in f.read_text(encoding="utf-8"))]
    assert carriers, (
        "no module under frontend/src/hooks/ or frontend/src/lib/ carries "
        "`error_code` onto the chat item any more, so a coded turn failure "
        "reaches the user as bare prose with no fix link. Looked in: "
        + ", ".join(f.name for f in modules))

    # The streamed path is a SECOND transport for the same failure, and it has
    # to carry the code too — `chatEvents.applyEvent` is where it lands there.
    events = (fe / "lib" / "chatEvents.ts")
    if events.exists():
        src = events.read_text(encoding="utf-8")
        assert "code: ev.code" in src, (
            "the streamed turn path drops the error code, so the same failure "
            "gets a fix link on the bridge path and none on the gateway path.")

    cotc = (fe / "components" / "chat" / "ChainOfThought.tsx").read_text(encoding="utf-8")
    assert "fixForCode" in cotc and "FixLink" in cotc, (
        "ChainOfThought no longer renders a fix-it link — this is the surface a "
        "failed first message lands on, and the one that had none")


def test_the_frontend_routes_model_unknown_somewhere_useful() -> None:
    """Operations is where `_down` goes and it is wrong for this: the engine is
    up, so there is nothing there to restart."""
    fixes = (ROOT / "frontend" / "src" / "lib" / "fixes.ts").read_text(encoding="utf-8")
    assert "model_unknown" in fixes
    i = fixes.index("model_unknown")
    assert "hub/agent" in fixes[i:i + 400], (
        "model_unknown no longer routes to Setup → Agent")
