"""Long-term memory: namespaced, permissioned, redacted, file-backed.

memory/*.yaml is the *definition* (committed). state/memory/ is the *data* (gitignored).

The reference backend is JSON lines with keyword-overlap scoring - dependency-free and
good enough to prove the wiring. Swap `_load`/`_append`/`_score` for a vector store
(pgvector, LanceDB, Qdrant...) without touching callers: the permission and redaction
rules live above the storage layer and must survive the swap.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .policy_engine import PolicyEngine
from .telemetry import utcnow
from .types import PolicyViolation

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "it", "this", "that", "with", "what", "how"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _age_days(entry: dict[str, Any]) -> float:
    created = datetime.fromisoformat(entry["created_at"])
    return max((datetime.now(UTC) - created).total_seconds() / 86400, 0.0)


class MemoryManager:
    def __init__(self, config: dict[str, dict[str, Any]], state_dir: Path, policy: PolicyEngine, session_id: str):
        self.settings = config.get("memory", {})
        self.retrieval = config.get("retrieval", {})
        self.consolidation = config.get("consolidation", {})
        self.namespaces: dict[str, dict[str, Any]] = config.get("namespaces", {}).get("namespaces", {})
        self.base = state_dir / "memory"
        self.policy = policy
        self.session_id = session_id

    def can(self, agent: str, namespace: str, mode: str) -> bool:
        spec = self.namespaces.get(namespace)
        return bool(spec) and bool({"*", agent} & set(spec.get(mode, [])))

    def _path(self, namespace: str, agent: str) -> Path:
        scope = self.namespaces[namespace].get("scope", "global")
        if scope == "session":
            return self.base / namespace / f"{self.session_id}.jsonl"
        if scope == "agent":
            return self.base / namespace / f"{agent}.jsonl"
        return self.base / f"{namespace}.jsonl"

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def remember(self, agent: str, namespace: str, text: str, tags: list[str] | None = None, task_id: str = "") -> dict[str, Any]:
        if not self.can(agent, namespace, "write"):
            raise PolicyViolation(f"{agent} may not write memory namespace {namespace!r} (memory/namespaces.yaml)")
        limit = self.settings.get("max_entry_chars", 2000)
        if len(text) > limit:
            raise PolicyViolation(f"memory entries are limited to {limit} characters; summarize first")
        if self.settings.get("redact_before_write", True):
            text = self.policy.redact(text)
        entry = {
            "id": uuid.uuid4().hex[:12],
            "namespace": namespace,
            "text": text,
            "tags": tags or [],
            "agent": agent,
            "task_id": task_id,
            "created_at": utcnow(),
        }
        path = self._path(namespace, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def _score(self, query_tokens: set[str], entry: dict[str, Any]) -> float:
        if not query_tokens:
            return 0.0
        overlap = len(query_tokens & _tokens(entry["text"] + " " + " ".join(entry.get("tags", []))))
        relevance = overlap / len(query_tokens)
        half_life = self.retrieval.get("recency_half_life_days", 30)
        weight = self.retrieval.get("recency_weight", 0.3)
        decay = math.pow(0.5, _age_days(entry) / half_life) if half_life else 1.0
        return relevance * ((1 - weight) + weight * decay)

    def recall(self, agent: str, query: str, namespaces: list[str], k: int | None = None) -> list[dict[str, Any]]:
        readable = [ns for ns in namespaces if self.can(agent, ns, "read")]
        q = _tokens(query)
        scored = [
            (self._score(q, e), e)
            for ns in readable
            for e in self._load(self._path(ns, agent))
        ]
        min_score = self.retrieval.get("min_score", 0.2)
        ranked = sorted((s for s in scored if s[0] >= min_score), key=lambda s: s[0], reverse=True)
        budget, out = self.retrieval.get("max_chars", 4000), []
        for score, entry in ranked[: k or self.retrieval.get("k", 5)]:
            budget -= len(entry["text"])
            if budget < 0:
                break
            out.append({**entry, "score": round(score, 3)})
        return out

    def consolidate(self) -> dict[str, int]:
        """Dedupe, expire (ttl_days) and cap every namespace file. Run on a schedule."""
        stats = {"files": 0, "removed": 0}
        cap = self.consolidation.get("max_entries_per_namespace", 1000)
        for ns, spec in self.namespaces.items():
            ttl = spec.get("ttl_days")
            target = self.base / f"{ns}.jsonl"
            paths = [target] if target.exists() else sorted((self.base / ns).glob("*.jsonl"))
            for path in paths:
                entries = self._load(path)
                kept: dict[str, dict[str, Any]] = {}
                for entry in entries:  # later entries win, so the newest copy of a duplicate survives
                    if ttl and _age_days(entry) > ttl:
                        continue
                    key = " ".join(entry["text"].lower().split()) if self.consolidation.get("dedupe", True) else entry["id"]
                    kept.pop(key, None)
                    kept[key] = entry
                survivors = list(kept.values())[-cap:]
                stats["files"] += 1
                stats["removed"] += len(entries) - len(survivors)
                body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in survivors)
                path.write_text(body, encoding="utf-8")
        return stats

    def prune_sessions(self, older_than_days: int) -> int:
        """Delete session-scoped files older than N days."""
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        removed = 0
        for ns, spec in self.namespaces.items():
            if spec.get("scope") != "session":
                continue
            for path in (self.base / ns).glob("*.jsonl"):
                if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
                    path.unlink()
                    removed += 1
        return removed
