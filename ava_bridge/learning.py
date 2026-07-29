"""Self-analysis / learning engine — separate contexts for code vs chat.

Code Learning: Analyzes Ava's code edits, commits, inline fixes. Proposes
  optimizations, new features, refactoring. ALL proposals require user approval.

Chat Learning: Analyzes user-Ava conversations, common topics, questions,
  patterns. Proposes new skills, documentation, capabilities. ALL proposals
  require user approval.

Inline Error Fixes: Automatic (no approval) unless marked critical.
  Critical errors (permission denied, path escapes, destructive ops) are
  flagged for manual review instead.

LOCAL-FIRST: cycle analysis is a summarization task, so it runs on Ava's own
router (the local Omni brain) and only falls back to the Anthropic key when the
router is unreachable — the learner is not cloud-dependent. Cycles run on an
in-process schedule (`start_scheduler`, see config `features.learning` /
`learning.interval_hours`) and on demand via `run_all_cycles()`.
"""
import asyncio
import json
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime

import httpx

from . import state, config, features


# --------------------------------------------------------------------------- #
# LLM access — local-first. Every path is best-effort: a learning cycle must
# NEVER raise into the scheduler or an API handler, so all failures return
# None/[] rather than propagating.
# --------------------------------------------------------------------------- #
async def _complete_local(prompt: str, max_tokens: int) -> str | None:
    """Ask Ava's own router (local Omni brain). Returns the reply text or None."""
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


def _cloud_fallback_enabled() -> bool:
    """May a cycle send its prompt off-box when the router can't answer?

    OFF by default, and deliberately a separate switch from `features.learning`.
    These prompts carry verbatim `User:`/`Ava:` excerpts from EVERY conversation,
    so "the local model was busy" must never silently become "your chats went to
    a third party". Turning learning on is a statement about self-analysis; it is
    not consent to upload the material being analysed.
    """
    return features.enabled("learning_cloud_fallback")


async def _complete_anthropic(prompt: str, max_tokens: int) -> str | None:
    """Fallback to the Anthropic key when the local router can't answer."""
    if not _cloud_fallback_enabled():
        return None
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        headers = {
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": config.ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body = {"model": config.CODE_MODEL, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(f"{config.ANTHROPIC_BASE}/v1/messages",
                                  headers=headers, json=body)
        if r.status_code != 200:
            return None
        resp = r.json()
        return next((b["text"] for b in resp.get("content", [])
                     if b.get("type") == "text"), "") or None
    except Exception:  # noqa: BLE001
        return None


def _parse_proposals(text: str, ptype: str) -> list[dict]:
    """Extract a JSON list of proposals from a model reply and normalize them."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        raw = json.loads(match.group())
    except (ValueError, TypeError):
        return []
    proposals = []
    for p in (raw if isinstance(raw, list) else [])[:3]:
        if not isinstance(p, dict):
            continue
        proposals.append({
            "id": f"prop_{uuid.uuid4().hex[:8]}",
            "title": p.get("title") or p.get("what") or "Untitled",
            "description": p.get("description") or p.get("what") or "",
            "why": p.get("why") or p.get("reason") or "",
            "risk": str(p.get("risk", "medium")).lower(),
            "effort": p.get("effort", ""),
            "type": ptype,
            "status": "pending",
            "requires_approval": True,
            "source": "learning",  # vs the code_agent audit log (source absent)
        })
    return proposals


async def _llm_propose(prompt: str, ptype: str, max_tokens: int = 1200) -> list[dict]:
    """Local-first proposal generation: router → Anthropic fallback → parse."""
    text = await _complete_local(prompt, max_tokens)
    if not text:
        text = await _complete_anthropic(prompt, max_tokens)
    if not text:
        return []
    return _parse_proposals(text, ptype)


class BaseLearner:
    """Base learner with common logic."""

    def __init__(self, context_name: str, state_dict: dict, state_lock):
        """
        Args:
            context_name: "code" or "chat"
            state_dict: The dict to use (code_learning_state or chat_learning_state)
            state_lock: The lock to use
        """
        self.context = context_name
        self.state_dict = state_dict
        self.state_lock = state_lock

    def record_inline_fix(
        self, error: str, fix_desc: str, retry_succeeded: bool, critical: bool = False
    ):
        """Log an inline error fix. If critical, it was flagged for approval instead of auto-applied."""
        with self.state_lock:
            self.state_dict["inline_fixes"].append(
                {
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "error": error[:100],
                    "fix_applied": fix_desc,
                    "retry_succeeded": retry_succeeded,
                    "critical": critical,
                }
            )
            # Keep only last 50 inline fixes
            if len(self.state_dict["inline_fixes"]) > 50:
                self.state_dict["inline_fixes"] = self.state_dict["inline_fixes"][-50:]

    def record_cycle(self, patterns: dict, proposals: list[dict]):
        """Save a learning cycle to this context's state."""
        with self.state_lock:
            cycle = {
                "id": f"cycle_{self.context}_{uuid.uuid4().hex[:8]}",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "context": self.context,
                "patterns": patterns,
                "proposals": proposals,
            }
            self.state_dict["cycles"].append(cycle)
            self.state_dict["last_cycle"] = cycle["timestamp"]
            # Keep only last 20 cycles
            if len(self.state_dict["cycles"]) > 20:
                self.state_dict["cycles"] = self.state_dict["cycles"][-20:]
        # Persist to disk
        state.save_learning_state()
        return cycle


class CodeLearner(BaseLearner):
    """Learns from Ava's code edits, commits, and modifications."""

    def __init__(self):
        super().__init__("code", state.code_learning_state, state.code_learning_state_lock)

    def analyze_code_turns(self, code_turns: list[dict], lookback_hours: int = 24) -> dict:
        """Extract patterns from recent code-mode turns.
        
        Looks at applied changes, file types, error patterns, commit messages.
        """
        if not code_turns:
            return {}

        patterns = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "code_turns_analyzed": len(code_turns),
            "lookback_hours": lookback_hours,
        }

        # Track what's being edited
        files_changed = Counter()
        file_types = Counter()
        errors = []
        changes_applied = 0
        changes_rejected = 0

        for ct in code_turns:
            if ct.get("applied"):
                changes_applied += 1
            if ct.get("status") == "error":
                changes_rejected += 1
                errors.append(ct.get("error", "unknown")[:60])

            # Count file changes by type
            for edit in ct.get("edits", []):
                path = edit.get("path", "")
                files_changed[path] += 1
                # Extract file extension
                ext = path.split(".")[-1] if "." in path else "no_ext"
                file_types[ext] += 1

        patterns["files_most_changed"] = dict(files_changed.most_common(5))
        patterns["file_types"] = dict(file_types.most_common(5))
        patterns["changes_applied"] = changes_applied
        patterns["changes_rejected"] = changes_rejected
        patterns["recent_errors"] = errors[:3]

        return patterns

    async def propose_code_improvements(self, patterns: dict) -> list[dict]:
        """Use Claude to suggest code improvements based on patterns.
        
        Analyzes changed files, error patterns, and suggests:
          - Refactoring opportunities
          - Performance optimizations
          - Test coverage for frequently-changed files
          - Code style/consistency improvements
        """
        if not patterns or not patterns.get("code_turns_analyzed", 0):
            return []

        prompt = f"""Based on my code changes, here are patterns:

Files most changed: {json.dumps(patterns.get('files_most_changed', {}), indent=2)}
File types: {json.dumps(patterns.get('file_types', {}), indent=2)}
Applied changes: {patterns.get('changes_applied')}
Rejected changes: {patterns.get('changes_rejected')}
Recent errors: {json.dumps(patterns.get('recent_errors', []), indent=2)}

Based on these patterns, suggest 2-3 code improvements. For each give:
1. "title": short name
2. "description": what to improve (file, function, architecture)
3. "why": performance, maintainability, testing
4. "risk": low/medium/high
5. "effort": minutes/hours

Reply with ONLY a JSON list of objects with those keys. Keep it concise."""

        return await _llm_propose(prompt, "code_improvement")

    async def run_learning_cycle(self, code_turns: list[dict]) -> dict:
        """Full cycle: analyze code patterns, propose improvements."""
        patterns = self.analyze_code_turns(code_turns)
        proposals = await self.propose_code_improvements(patterns)
        cycle = self.record_cycle(patterns, proposals)
        return cycle


class ChatLearner(BaseLearner):
    """Learns from general chat conversations."""

    def __init__(self):
        super().__init__("chat", state.chat_learning_state, state.chat_learning_state_lock)

    def analyze_chat_history(self, turns: list[dict], lookback_hours: int = 24) -> dict:
        """Extract patterns from recent chat turns."""
        if not turns:
            return {}

        patterns = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "chat_turns_analyzed": len(turns),
            "lookback_hours": lookback_hours,
        }

        topics = Counter()
        slow_queries = []
        errors = []
        tool_calls = Counter()

        for t in turns:
            msg = t.get("user_message", "")
            if msg:
                words = msg.lower().split()
                topic = next((w for w in words if len(w) > 3), "general")
                topics[topic] += 1

            latency_ms = t.get("latency_ms", 0)
            if latency_ms > 3000:
                slow_queries.append(
                    {
                        "query": msg[:50],
                        "latency_ms": latency_ms,
                        "timestamp": t.get("timestamp"),
                    }
                )

            if t.get("error"):
                errors.append(
                    {"error": t["error"], "query": msg[:50], "timestamp": t.get("timestamp")}
                )

            if t.get("tools_used"):
                tool_calls.update(t["tools_used"])

        patterns["common_topics"] = dict(topics.most_common(5))
        patterns["slow_queries"] = slow_queries[:3]
        patterns["errors"] = errors[:3]
        patterns["tools_usage"] = dict(tool_calls.most_common(5))

        # Detect capability gaps
        gap_keywords = r"\b(don't have|no tool|can't|missing|unable to|not supported)\b"
        gaps = []
        for e in errors:
            if re.search(gap_keywords, e.get("error", ""), re.I):
                gaps.append(e["error"][:60])
        patterns["capability_gaps"] = gaps

        return patterns

    async def propose_chat_improvements(self, patterns: dict) -> list[dict]:
        """Use Claude to suggest improvements based on chat patterns.
        
        Proposes:
          - New skills or tools for common requests
          - Documentation on frequently-asked topics
          - Configuration tuning for slow queries
        """
        if not patterns or not patterns.get("chat_turns_analyzed", 0):
            return []

        prompt = f"""Based on conversations, here are patterns I've noticed:

Common topics: {json.dumps(patterns.get('common_topics', {}), indent=2)}
Slow queries (>3s): {json.dumps(patterns.get('slow_queries', []), indent=2)}
Recent errors: {json.dumps(patterns.get('errors', []), indent=2)}
Tool usage: {json.dumps(patterns.get('tools_usage', {}), indent=2)}
Capability gaps: {json.dumps(patterns.get('capability_gaps', []), indent=2)}

Suggest 2-3 improvements to help me better serve users. For each give:
1. "title": short name
2. "description": what to add (skill, tool, documentation, config change)
3. "why": UX, capability, performance
4. "risk": low/medium/high
5. "effort": minutes/hours

Reply with ONLY a JSON list of objects with those keys."""

        return await _llm_propose(prompt, "chat_improvement")

    async def run_learning_cycle(self, turns: list[dict]) -> dict:
        """Full cycle: analyze chat patterns, propose improvements."""
        patterns = self.analyze_chat_history(turns)
        proposals = await self.propose_chat_improvements(patterns)
        cycle = self.record_cycle(patterns, proposals)
        return cycle


class MemoryDistiller:
    """Distills durable facts about the owner from recent chat history into
    the long-term memory store (ava_bridge/memory_store.py).

    Unlike the code/chat learners this produces memories, not approval
    proposals — its governance surface is the Hub Memory tab (view / edit /
    delete / export) plus a `memory_distill` audit event per cycle. A kv
    cursor in memory.db marks how far distillation has read, so each cycle
    only sees new messages."""

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
            text = await _complete_anthropic(prompt, 800)
        if not text:
            return 0  # keep the cursor — retry these messages next cycle

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


# Singleton learner instances
code_learner = CodeLearner()
chat_learner = ChatLearner()
memory_distiller = MemoryDistiller()


# --------------------------------------------------------------------------- #
# Orchestration + in-process scheduler
# --------------------------------------------------------------------------- #
async def run_all_cycles() -> dict:
    """Run one code + one chat learning cycle from live in-memory activity.

    Best-effort and self-contained — safe to call from the scheduler or an API
    handler. Reads chat turns from state.turns and code turns from
    state.code_turns, persists any proposals, and returns a small summary."""
    with state.turns_lock:
        chat_turns = list(state.turns.values())
    with state.code_turns_lock:
        code_activity = list(state.code_turns.values())

    summary = {"ran": True, "code_proposals": 0, "chat_proposals": 0,
               "chat_turns": len(chat_turns), "code_turns": len(code_activity)}
    try:
        cyc = await code_learner.run_learning_cycle(code_activity)
        summary["code_proposals"] = len(cyc.get("proposals", []))
    except Exception:  # noqa: BLE001
        summary["code_error"] = True
    try:
        cyc = await chat_learner.run_learning_cycle(chat_turns)
        summary["chat_proposals"] = len(cyc.get("proposals", []))
    except Exception:  # noqa: BLE001
        summary["chat_error"] = True
    try:
        summary["memory_facts"] = await memory_distiller.run_cycle()
    except Exception:  # noqa: BLE001
        summary["memory_error"] = True
    return summary


_SCHED_STARTED = False


def _scheduler_loop() -> None:
    interval = max(3600.0, config.LEARNING_INTERVAL_H * 3600.0)
    while True:
        time.sleep(interval)  # first cycle after one interval — no LLM call at boot
        try:
            asyncio.run(run_all_cycles())
        except Exception:  # noqa: BLE001 — a learning failure must never take the bridge down
            pass


def start_scheduler() -> None:
    """Start the in-process learning scheduler once (idempotent). No-op when
    learning is disabled (`features.learning: false`). Portable — no systemd
    timer; runs the same on a host, in Docker, or on a Mac."""
    global _SCHED_STARTED
    if _SCHED_STARTED or not config.LEARNING_ENABLED:
        return
    _SCHED_STARTED = True
    threading.Thread(target=_scheduler_loop, daemon=True, name="ava-learning").start()
