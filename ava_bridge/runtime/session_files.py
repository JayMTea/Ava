"""Resolve CLI session keys after OpenClaw rotates their transcript files."""
import shlex
import subprocess
from pathlib import PurePosixPath


def resolve_transcript(exec_command, fallback: str, agent: str, session_id: str) -> str:
    # The old named transcript can still exist. Follow only this exact session's
    # index entry, never the newest file (which could belong to another chat).
    root = str(PurePosixPath(fallback).parent)
    key = f"agent:{agent}:explicit:{session_id}"
    script = (
        "import json, pathlib; "
        f"root=pathlib.Path({root!r}); "
        "index=json.loads((root/'sessions.json').read_text()); "
        f"entry=index.get({key!r}, {{}}); "
        "path=pathlib.Path(entry.get('sessionFile') or "
        "str(root/(str(entry.get('sessionId') or '')+'.jsonl'))); "
        f"print(str(path) if path.parent == root and path.suffix == '.jsonl' "
        f"and path.is_file() else {fallback!r})"
    )
    try:
        path = exec_command(f"python3 -c {shlex.quote(script)}").strip()
        if (PurePosixPath(path).parent == PurePosixPath(root)
                and path.endswith('.jsonl') and '\n' not in path):
            return path
    except (OSError, subprocess.SubprocessError):  # Unavailable runtime or session index.
        pass
    return fallback
