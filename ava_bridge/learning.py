"""Recursive learning engine — separate contexts for code-mode vs chat.

Code Learning: Analyzes Ava's code edits, commits, inline fixes. Proposes
  optimizations, new features, refactoring. ALL proposals require user approval.

Chat Learning: Analyzes user-Ava conversations, common topics, questions,
  patterns. Proposes new skills, documentation, capabilities. ALL proposals
  require user approval.

Inline Error Fixes: Automatic (no approval) unless marked critical.
  Critical errors (permission denied, path escapes, destructive ops) are
  flagged for manual review instead.
"""
import json
import re
import uuid
from collections import Counter
from datetime import datetime

from . import state, config


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

Based on these patterns, suggest 2-3 code improvements:
1. What to improve (file, function, architecture)
2. Why (performance, maintainability, testing)
3. Risk level: low/medium/high
4. Estimated effort: minutes/hours

Format as JSON list. Keep descriptions concise."""

        try:
            if not config.ANTHROPIC_API_KEY:
                return []

            import httpx

            headers = {
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": config.ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
            body = {
                "model": config.CODE_MODEL,
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            }
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{config.ANTHROPIC_BASE}/v1/messages",
                    headers=headers,
                    json=body,
                )
                if r.status_code != 200:
                    return []
                resp = r.json()

            text = next(
                (block["text"] for block in resp.get("content", []) if block.get("type") == "text"),
                "",
            )
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if not match:
                return []
            proposals_raw = json.loads(match.group())

            proposals = []
            for p in proposals_raw[:3]:
                proposals.append(
                    {
                        "id": f"prop_{uuid.uuid4().hex[:8]}",
                        "title": p.get("title", "Untitled"),
                        "description": p.get("description", ""),
                        "why": p.get("why", ""),
                        "risk": p.get("risk", "medium").lower(),
                        "effort": p.get("effort", ""),
                        "type": "code_improvement",
                        "code_turn_id": None,
                        "status": "pending",
                        "requires_approval": True,
                    }
                )
            return proposals
        except Exception:
            return []

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

Suggest 2-3 improvements to help me better serve users:
1. What to add (skill, tool, documentation, config change)
2. Why this matters (UX, capability, performance)
3. Risk level: low/medium/high
4. Estimated effort: minutes/hours

Format as JSON list."""

        return []

    async def run_learning_cycle(self, turns: list[dict]) -> dict:
        """Full cycle: analyze chat patterns, propose improvements."""
        patterns = self.analyze_chat_history(turns)
        proposals = await self.propose_chat_improvements(patterns)
        cycle = self.record_cycle(patterns, proposals)
        return cycle


# Singleton learner instances
code_learner = CodeLearner()
chat_learner = ChatLearner()
