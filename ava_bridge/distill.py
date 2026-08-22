"""Memory distillation — durable facts about the owner, mined from chat history.

Extracted from the retired `learning.py`. Distillation used to be one of three
things the learning scheduler ran, which meant the capability the README leads
with ("It remembers") was switched on by a feature flag named `learning` and
died with it. It is its own module and its own scheduler now, gated on the
switch that actually describes it (`features.memory`).

Unlike the retired learners this produces MEMORIES, not approval proposals: its
governance surface is the Hub Memory tab (view / edit / delete / export) plus a
`memory_distill` audit event per cycle. A kv cursor in memory.db marks how far
distillation has read, so each cycle only sees new messages.

LOCAL-ONLY. The prompt carries verbatim `User:`/`Ava:` excerpts from real
conversations, so there is deliberately no cloud fallback: if Ava's own router
cannot answer, the cycle does nothing and the cursor stays put. The old
Anthropic fallback went with `ANTHROPIC_API_KEY` when self-editing was removed,
and it should not come back — "the local model was busy" must never silently
become "your chats went to a third party".
"""
import asyncio
import json
import re
import threading
import time

import httpx

from . import config


async def _complete_local(prompt: str, max_tokens: int) -> str | None:
    """Ask Ava's own router (local Omni brain). Returns the reply text or None.

    Best-effort by contract: a distillation cycle must NEVER raise into the
    scheduler or an API handler, so every failure returns None rather than
    propagating.
    """
    try:
        headers = {}
        if config.ROUTER_TOKEN:
            headers["X-Ava-Router-Token"] = config.ROUTER_TOKEN
        if config.INFERENCE_KEY:  # cloud-backed router auth, if configured
            headers["Authorization"] = "Bearer " + config.INFERENCE_KEY
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                config.ROUTER_CHAT_URL, headers=headers,
                json={"messages": [{"role": "user", "content": prompt}],
                      "max_tokens": max_tokens, "stream": False},
            )
        if r.status_code != 200:
            return None
        data = r.json()
        text = (((data.get("choices") or [{}])[0].get("message") or {})
                .get("content") or "").strip()
        return text or None
    except Exception:  # noqa: BLE001
        return None


class MemoryDistiller:
    """Distills durable facts about the owner from recent chat history into
    the long-term memory store (ava_bridge/memory_store.py)."""

    KV_KEY = "distill_last_ts"
    MAX_FACTS = 8
    MIN_MESSAGES = 4  # below this there's no signal worth an LLM call

    @staticmethod
    def _transcript(msgs: list[dict], per_msg: int = 500, total: int = 6000) -> str:
        lines = []
        for m in msgs:
            who = "User" if m["role"] == "user" else "Ava"
            lines.append(f"{who}: {m['content'][:per_msg]}")
        text = "\n".join(lines)
        return text[-total:]

    @staticmethod
    def _parse_facts(text: str) -> list[str]:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            raw = json.loads(match.group())
        except (ValueError, TypeError):
            return []
        return [s.strip()[:300] for s in raw
                if isinstance(s, str) and s.strip()]

    async def run_cycle(self) -> int:
        """One distillation pass over unseen chat messages. Returns the number
        of facts stored (0 on no signal / no LLM / nothing durable)."""
        from . import chat_store, memory_store
        if not config.MEMORY_ENABLED:
            return 0
        last = float(memory_store.kv_get(self.KV_KEY, "0") or 0)
        msgs = chat_store.recent_messages(last)
        if len(msgs) < self.MIN_MESSAGES:
            return 0

        prompt = f"""Below is a recent conversation log between a user and their assistant.

{self._transcript(msgs)}

Extract facts about the USER worth remembering long-term: stable preferences,
ongoing projects, their setup/hardware, people or places they mention
recurringly, decisions they made. Only durable facts — skip one-off tasks,
small talk, and anything the assistant said about itself. Write each fact as
one self-contained sentence.

Reply with ONLY a JSON array of strings (max {self.MAX_FACTS}), or [] if
nothing qualifies."""

        text = await _complete_local(prompt, 800)
        if not text:
            return 0  # keep the cursor — retry these messages next cycle

        # Re-read the cursor after the await. run_distill_cycle is single-flight,
        # so this should not fire — but the read at the top of this method and the
        # write at the bottom straddle a 45s network call, and this is the only
        # thing that makes the pair safe if a second entry point is ever added
        # (an `ava` CLI subcommand, a cron script calling the distiller directly).
        # Bailing here loses nothing: the messages stay unprocessed and the next
        # cycle picks them up from wherever the other run left the cursor.
        if float(memory_store.kv_get(self.KV_KEY, "0") or 0) != last:
            return 0

        n = 0
        for fact in self._parse_facts(text)[:self.MAX_FACTS]:
            if memory_store.add("fact", fact, source="distilled") is not None:
                n += 1
        # Advance the cursor now that an LLM actually looked at these messages
        # (even if it found nothing durable).
        memory_store.kv_set(self.KV_KEY, str(msgs[-1]["ts"]))
        if n:
            from . import audit
            audit.record("memory_distill", added=n, messages=len(msgs))
        return n


memory_distiller = MemoryDistiller()


# --------------------------------------------------------------------------- #
# Orchestration + in-process scheduler
# --------------------------------------------------------------------------- #
# Single-flight claim — see run_distill_cycle for why this is a threading
# primitive and not an asyncio one.
_CYCLE_LOCK = threading.Lock()


async def run_distill_cycle() -> dict:
    """Run one distillation cycle. Safe to call from the scheduler or a handler.

    Single-flight: a second caller is REFUSED, not queued. A cycle spends most
    of its life awaiting a 45s LLM call, so two entry points overlapping is an
    ordinary event, not a race you need load to hit — both runs would read the
    same cursor, spend the same 45s on the same messages, and then both advance
    it.

    A threading.Lock rather than an asyncio.Lock on purpose: the scheduler calls
    this through `asyncio.run(...)` on its own daemon thread (_scheduler_loop
    below), so two callers need not share an event loop and an asyncio primitive
    would not serialise them at all. Acquired non-blocking, so no caller waits.
    """
    if not _CYCLE_LOCK.acquire(blocking=False):
        return {"ran": False, "reason": "a distillation cycle is already running"}
    try:
        try:
            return {"ran": True, "memory_facts": await memory_distiller.run_cycle()}
        except Exception:  # noqa: BLE001 — never raise into a scheduler/handler
            return {"ran": True, "memory_error": True}
    finally:
        _CYCLE_LOCK.release()


_SCHED_STARTED = False


def _scheduler_loop() -> None:
    interval = max(3600.0, config.MEMORY_DISTILL_INTERVAL_H * 3600.0)
    while True:
        time.sleep(interval)  # first cycle after one interval — no LLM call at boot
        try:
            asyncio.run(run_distill_cycle())
        except Exception:  # noqa: BLE001 — a failure must never take the bridge down
            pass


def start_scheduler() -> None:
    """Start the in-process distillation scheduler once (idempotent). No-op when
    memory is disabled (`features.memory: false`). Portable — no systemd timer;
    runs the same on a host, in Docker, or on a Mac."""
    global _SCHED_STARTED
    if _SCHED_STARTED or not config.MEMORY_ENABLED:
        return
    _SCHED_STARTED = True
    threading.Thread(target=_scheduler_loop, daemon=True, name="ava-distill").start()
