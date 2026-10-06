"""Per-turn checkpoints so a crashed or interrupted run can resume.

state/checkpoints/<task_id>/turn-NNNN.json holds the full message history (including the
provider's raw blocks, which must be replayed verbatim). Only the newest `keep` files are
retained.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .types import Message, Usage


class CheckpointManager:
    def __init__(self, state_dir: Path, keep: int = 3):
        self.dir = state_dir / "checkpoints"
        self.keep = keep

    def save(self, task_id: str, turn: int, messages: list[Message], usage: Usage, tool_calls: int) -> Path:
        folder = self.dir / task_id
        folder.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": task_id,
            "turn": turn,
            "tool_calls": tool_calls,
            "usage": vars(usage),
            "messages": [m.to_dict() for m in messages],
        }
        path = folder / f"turn-{turn:04d}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)
        for old in sorted(folder.glob("turn-*.json"))[: -self.keep]:
            old.unlink()
        return path

    def latest(self, task_id: str) -> dict[str, Any] | None:
        files = sorted((self.dir / task_id).glob("turn-*.json"))
        if not files:
            return None
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        payload["messages"] = [Message.from_dict(m) for m in payload["messages"]]
        payload["usage"] = Usage(**payload["usage"])
        return payload
