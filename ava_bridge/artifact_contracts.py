"""Application-neutral, bounded chart contracts with explicit legacy adapters."""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from urllib.parse import unquote, urlsplit

from .artifact_compat import MAX_BYTES, validate as validate_legacy


def _citations(value) -> None:
    if not isinstance(value, list) or len(value) > 1000 or any(
        not isinstance(row, dict) or not isinstance(row.get("url"), str)
        or not isinstance(row.get("title", ""), str) for row in value
    ):
        raise ValueError("Invalid source citations")


def _local_path(path: str) -> bool:
    if not isinstance(path, str) or len(path) > 64000:
        return False
    decoded = path
    for _ in range(4):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    url = urlsplit(decoded)
    return (decoded.startswith("/") and not decoded.startswith("//")
            and not url.scheme and not url.netloc and not url.fragment
            and "\\" not in decoded and "%" not in url.path
            and not any(ord(c) < 32 for c in decoded)
            and not any(part in (".", "..") for part in url.path.split("/")))


def validate(artifact: dict, cid: str = "") -> None:
    if artifact.get("schema_version") != "ava-artifact/3":
        validate_legacy(artifact)
        return
    if len(json.dumps(artifact, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError("Artifact exceeds the snapshot budget")
    if artifact.get("type") != "analytics" or artifact.get("mode") not in ("live", "snapshot"):
        raise ValueError("Invalid artifact type or mode")
    uuid.UUID(str(artifact.get("id")))
    if not isinstance(artifact.get("title"), str) or not 1 <= len(artifact["title"]) <= 1000:
        raise ValueError("Invalid artifact title")
    if artifact["mode"] == "live":
        from . import connectors
        visual, chart = artifact.get("visualization"), artifact.get("chart")
        if not isinstance(visual, dict) or visual.get("format") != "app" or not _local_path(visual.get("path")):
            raise ValueError("Live artifacts must name a safe app-relative destination")
        manifest = next((m for m in connectors.load() if m.get("id") == cid), {})
        declared = manifest.get("x_artifacts")
        prefixes = declared.get("live_paths", []) if isinstance(declared, dict) else []
        path = urlsplit(unquote(visual["path"])).path
        if not isinstance(prefixes, list) or not any(
            isinstance(prefix, str) and _local_path(prefix) and "?" not in prefix
            and (path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/"))
            for prefix in prefixes
        ):
            raise ValueError("Live artifact destination is not declared in x_artifacts.live_paths")
        if not isinstance(chart, dict):
            raise ValueError("Live artifact requires a saved chart description")
        _citations(chart.get("citations"))
        if not isinstance(chart.get("sources_in_view", False), bool):
            raise ValueError("Invalid chart sources_in_view")
        return
    result = artifact.get("result")
    if not isinstance(result, dict) or result.get("schema_version") != "analysis-result/2":
        raise ValueError("Snapshot requires analysis-result/2")
    if result.get("id") != artifact["id"] or result.get("mode") != "snapshot":
        raise ValueError("Snapshot identity differs")
    if artifact.get("chart_type") not in ("bar", "table"):
        raise ValueError("Use bar or table for a recorded result")
    for key in ("title", "unit", "dimension_label", "created_at", "method"):
        if not isinstance(result.get(key), str) or not 1 <= len(result[key]) <= 65536:
            raise ValueError(f"Invalid result {key}")
    if "scope_label" in result and not isinstance(result["scope_label"], str):
        raise ValueError("Invalid result scope_label")
    datetime.fromisoformat(result["created_at"])
    rows, columns = result.get("rows"), result.get("columns")
    if not isinstance(rows, list) or len(rows) > 5000 or result.get("row_count") != len(rows):
        raise ValueError("Invalid result rows")
    if not isinstance(columns, list) or not columns or len(columns) > 100 or any(
        not isinstance(c, dict) or not isinstance(c.get("name"), str) for c in columns
    ):
        raise ValueError("Invalid result columns")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            raise ValueError("A result row must have a label")
        value = row.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError("Invalid result value")
        uncertainty = row.get("moe_90")
        if uncertainty is not None and (isinstance(uncertainty, bool)
                or not isinstance(uncertainty, (int, float)) or not math.isfinite(uncertainty)
                or uncertainty < 0):
            raise ValueError("Invalid result uncertainty")
    _citations(result.get("citations"))
    _citations(result.get("sources"))
    if not isinstance(result.get("limitations"), list) or not all(isinstance(x, str) for x in result["limitations"]):
        raise ValueError("Invalid result limitations")
    if artifact.get("visualization") is not None:
        from .data_artifacts import validate_visualization
        validate_visualization(artifact["visualization"], artifact["id"])
