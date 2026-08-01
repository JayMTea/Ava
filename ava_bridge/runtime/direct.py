"""DirectRuntime — the graceful floor when no agent runtime is present.

Talks to the inference endpoint directly (OpenAI-compatible), giving a working
but TOOL-LESS assistant: no sandbox, no tools/skills, no persistent agent memory
(it replays recent chat history for continuity). This is the explicit opt-out
(config `agent.required: false`) so a fresh install still works and non-supported
hardware isn't a dead end — not a silent replacement for the full runtime.
"""
from __future__ import annotations

import requests

from .base import AgentRuntime
from .. import config


class InferenceError(RuntimeError):
    """A failed turn that knows WHY, so the UI can offer somewhere to go.

    `code` follows the same shape features.preflight already emits, which is what
    frontend/src/lib/fixes.ts pattern-matches on: `<key>_down` routes to
    Operations with no frontend change at all. `model_unknown` is the one new
    pattern, because "the engine is fine, it just does not have that model" is
    the single most likely first-run failure and Operations is the wrong place
    to send someone for it.
    """

    def __init__(self, message: str, code: str = "inference_down",
                 status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


def _upstream_message(r) -> str:
    """What the ENGINE said, not what the hop to it looked like.

    raise_for_status() produced "404 Client Error: Not Found for url:
    http://127.0.0.1:8010/v1/chat/completions" — the router's own loopback
    address, which is neither where the problem is nor anywhere the owner can
    act. Ollama's body says `model 'llama3.2' not found`; that is the sentence
    worth keeping, and it was being discarded.
    """
    try:
        body = r.json()
    except Exception:  # noqa: BLE001 — HTML error page, or nothing at all
        return (getattr(r, "text", "") or "").strip()[:300]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "").strip()[:300]
        if isinstance(err, str) and err.strip():
            return err.strip()[:300]
        if body.get("detail"):
            return str(body["detail"]).strip()[:300]
    return (getattr(r, "text", "") or "").strip()[:300]


_MISSING_MODEL_HINTS = ("not found", "does not exist", "not_found",
                        "no such model", "unknown model")


def _inference_error(r) -> InferenceError:
    msg = _upstream_message(r)
    low = msg.lower()
    code = "inference_down"
    if "model" in low and any(h in low for h in _MISSING_MODEL_HINTS):
        code = "model_unknown"
    return InferenceError(
        msg or f"the inference endpoint returned HTTP {r.status_code}",
        code=code, status=getattr(r, "status_code", 0))


class DirectRuntime(AgentRuntime):
    name = "direct"
    supports_tools = False
    supports_cot = False

    def available(self) -> bool:
        return True  # only needs the inference endpoint, checked at call time

    def provision(self, auto_install: bool = False, scope: str = "all",
                  on_line=None) -> dict:
        """There is nothing to provision, and saying so is not the same as `ok`.

        The base class returns a cheerful `{"ok": True, "detail": "no provisioning
        needed"}`, which is true for a runtime that has no sandbox — but as the
        answer to an owner who just clicked Apply expecting their persona to
        reach Ava, it is a small lie. Direct has no sandbox, so nothing they
        edited went anywhere.
        """
        return {
            "ok": False,
            "steps": [],
            "scope": scope,
            "error_code": "direct_runtime_has_no_sandbox",
            "detail": ("the tool-less direct runtime has no sandbox to deploy "
                       "into — persona, skills and policies only reach an agent "
                       "runtime. Set agent.runtime to nemoclaw and provision it."),
        }

    def _system_prompt(self) -> str:
        return (f"You are {config.AVA_NAME}, {config.AVA_TAGLINE}. You run locally "
                "and privately on the user's own hardware. Be warm, concise, direct, "
                "and helpful. (Tool use is unavailable in this lightweight mode.)")

    def run_turn(self, text: str, session_id: str | None = None,
                 history: list[dict] | None = None) -> tuple[str, list[str]]:
        messages = [{"role": "system", "content": self._system_prompt()}]
        for m in (history or []):
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": text})
        headers = {"X-Ava-Router-Token": config.ROUTER_TOKEN} if config.ROUTER_TOKEN else {}
        if config.INFERENCE_KEY:   # cloud endpoint (OpenAI/OpenRouter/…) auth
            headers["Authorization"] = "Bearer " + config.INFERENCE_KEY
        r = requests.post(config.ROUTER_CHAT_URL,
                          json={"messages": messages, "stream": False},
                          headers=headers, timeout=config.OC_TIMEOUT)
        if not r.ok:
            raise _inference_error(r)
        data = r.json()
        reply = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not reply:
            raise RuntimeError("empty completion from inference endpoint")
        return reply, []

    def status(self) -> dict:
        return {"name": self.name, "available": True, "endpoint": config.ROUTER_CHAT_URL,
                "tools": False}
