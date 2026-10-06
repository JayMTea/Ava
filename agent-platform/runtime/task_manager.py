"""Task records: one JSON file per task under state/tasks/, validated against
schemas/task.schema.json on every write. Delegated sub-tasks link to their parent.

Lifecycle: pending -> running -> (awaiting_approval) -> completed | failed | cancelled
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import schema_validator
from .telemetry import utcnow
from .types import Usage

TERMINAL = {"completed", "failed", "cancelled"}


class TaskManager:
    def __init__(self, state_dir: Path, schemas_dir: Path, workspace: Path):
        self.dir = state_dir / "tasks"
        self.workspace = workspace
        self.task_validator = schema_validator(schemas_dir, "task")
        self.artifact_validator = schema_validator(schemas_dir, "artifact")

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _write(self, record: dict[str, Any]) -> dict[str, Any]:
        record["updated_at"] = utcnow()
        self.task_validator.validate(record)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(record["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(record["id"]))  # atomic on the same filesystem
        return record

    def create(self, agent: str, task_input: str, *, session_id: str, parent_id: str | None = None, workflow: str | None = None) -> dict[str, Any]:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        record = {
            "id": f"task_{stamp}_{uuid.uuid4().hex[:6]}",
            "agent": agent,
            "input": task_input,
            "status": "pending",
            "session_id": session_id,
            "parent_id": parent_id,
            "workflow": workflow,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "result": None,
            "error": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "artifacts": [],
        }
        return self._write(record)

    def get(self, task_id: str) -> dict[str, Any]:
        return json.loads(self._path(task_id).read_text(encoding="utf-8"))

    def update(self, task_id: str, **fields: Any) -> dict[str, Any]:
        record = self.get(task_id)
        record.update(fields)
        return self._write(record)

    def finish(self, task_id: str, status: str, *, result: str | None, error: str | None, usage: Usage) -> dict[str, Any]:
        assert status in TERMINAL, status
        return self.update(
            task_id,
            status=status,
            result=result,
            error=error,
            usage={"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
        )

    def record_artifact(self, task_id: str, agent: str, path: Path, artifact_type: str, description: str = "") -> dict[str, Any]:
        rel = path.relative_to(self.workspace).as_posix()
        artifact = {
            "id": f"art_{uuid.uuid4().hex[:10]}",
            "type": artifact_type,
            "path": rel,
            "task_id": task_id,
            "agent": agent,
            "created_at": utcnow(),
            "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "description": description,
        }
        self.artifact_validator.validate(artifact)
        record = self.get(task_id)
        record["artifacts"].append(artifact)
        self._write(record)
        return artifact

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        paths = sorted(self.dir.glob("task_*.json"), reverse=True)[:limit]
        return [json.loads(p.read_text(encoding="utf-8")) for p in paths]
