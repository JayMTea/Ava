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


class DirectRuntime(AgentRuntime):
    name = "direct"
    supports_tools = False
    supports_cot = False

    def available(self) -> bool:
        return True  # only needs the inference endpoint, checked at call time

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
        r.raise_for_status()
        data = r.json()
        reply = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not reply:
            raise RuntimeError("empty completion from inference endpoint")
        return reply, []

    def status(self) -> dict:
        return {"name": self.name, "available": True, "endpoint": config.ROUTER_CHAT_URL,
                "tools": False}
