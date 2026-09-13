"""Capture typed artifacts from authenticated connector results, never model HTML.

The model receives a compact reference. The full immutable snapshot is served
through Ava's existing authenticated browser boundary and survives chat reloads.
"""

import csv
import contextlib
import io
import json
import os
import re
import sqlite3
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from . import connectors, features, settings

router = APIRouter()
MAX_BYTES = 4 * 1024 * 1024
RECEIPT_PATTERN = re.compile(r'"ava_artifact_id"\s*:\s*"([a-f0-9-]{36})"')


@contextlib.contextmanager
def _db():
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)
    connection = sqlite3.connect(path, timeout=10)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > 1:
        connection.close()
        raise ValueError("Artifact database was written by a newer Ava version")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("CREATE TABLE IF NOT EXISTS artifact "
                       "(id TEXT PRIMARY KEY, connector TEXT NOT NULL, created REAL NOT NULL, "
                       "reference TEXT NOT NULL, payload TEXT NOT NULL)")
    if version == 0:
        connection.execute("PRAGMA user_version=1")
    days = settings.get_int("data.artifact_retention_days", 0)
    if days > 0:
        removed = connection.execute("DELETE FROM artifact WHERE created < ?",
                                     (time.time() - days * 86400,)).rowcount
        connection.commit()
        if removed:
            from . import audit
            audit.record("data_delete", store="artifacts", rows=removed, reason="retention")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def db_path() -> str:
    return os.path.join(settings.data_dir(), "analytics-artifacts.db")


def delete_all() -> int:
    with _db() as connection:
        count = connection.execute("DELETE FROM artifact").rowcount
    from . import audit
    audit.record("data_delete", store="artifacts", rows=count, reason="owner")
    return count


def inventory() -> dict:
    path = db_path()
    if not os.path.isfile(path):
        return {"path": path, "bytes": 0, "rows": 0, "last_write": 0}
    # Inventory is read-only; it neither creates a database nor prunes history.
    from pathlib import Path
    from contextlib import closing
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute("SELECT count(*) FROM artifact").fetchone()[0]
    return {"path": path, "bytes": os.path.getsize(path), "rows": rows,
            "last_write": os.path.getmtime(path)}


def _validate(artifact: dict, cid: str = "") -> None:
    from .artifact_contracts import validate
    validate(artifact, cid)


def validate_visualization(visualization, result_id: str) -> None:
    """Only inert SVG drawings bound to this recorded result can be served as images."""
    if (not isinstance(visualization, dict) or visualization.get("format") != "svg"
            or visualization.get("result_id") != result_id):
        raise ValueError("Chart and recorded result identities differ")
    content = visualization.get("content")
    if not isinstance(content, str) or len(content) > MAX_BYTES or "<!" in content:
        raise ValueError("Invalid chart image")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Invalid chart image") from exc
    namespace = "{http://www.w3.org/2000/svg}"
    tags = {"svg", "title", "desc", "rect", "path", "line", "circle", "g", "text", "tspan"}
    attributes = {"viewBox", "x", "y", "x1", "y1", "x2", "y2", "width", "height", "rx",
                  "ry", "cx", "cy", "r", "d", "fill", "stroke", "stroke-width", "font-family",
                  "font-size", "font-weight", "text-anchor", "transform", "opacity"}
    if root.tag != namespace + "svg":
        raise ValueError("Invalid chart image")
    for element in root.iter():
        if element.tag not in {namespace + tag for tag in tags}:
            raise ValueError("Chart contains unsupported elements")
        for key, value in element.attrib.items():
            if key not in attributes or "url(" in value.lower():
                raise ValueError("Chart contains unsupported attributes")


def capture(cid: str, data):
    """Called only after the connector's consent gate and authenticated tool call."""
    if not isinstance(data, dict):
        return data
    metadata = data.get("_meta")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("ava/artifact"), dict):
        return data
    # Large UI metadata never goes to the sandbox, including with the feature off.
    result = dict(data.get("structuredContent") or {})
    artifact = metadata["ava/artifact"]
    reference = None
    if features.preflight("data_artifacts") is None:
        _validate(artifact, cid)
        ident = str(uuid.uuid4())
        reference = {"type": "analytics", "id": ident, "ava_artifact_id": ident,
                     "result_id": artifact.get('result_id', artifact.get('id')),
                     "title": str(artifact.get("title", "Analysis"))[:240],
                     "connector_id": cid, "mode": artifact['mode'], "chart_type": artifact.get("chart_type", "bar")}
        # The connected app's configured UI is the only authority for its route.
        app = next((app for app in connectors.apps() if app["id"] == cid), {})
        artifact = {**artifact, "connector_id": cid, "app_url": app.get("url")}
        with _db() as connection:
            connection.execute("INSERT INTO artifact VALUES(?,?,?,?,?)",
                               (ident, cid, time.time(), json.dumps(reference),
                                json.dumps(artifact, ensure_ascii=False, allow_nan=False)))
    result.pop("artifact", None)
    result.pop("visualization", None)
    result.pop("ava_artifact_id", None)
    if reference:
        result = {"ava_artifact_id": reference["id"], "artifact": reference, **result}
    return {"structuredContent": result, "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": bool(data.get("isError"))}


def read(ident: str) -> tuple[dict, dict] | None:
    try:
        ident = str(uuid.UUID(ident))
    except (TypeError, ValueError):
        return None
    with _db() as connection:
        row = connection.execute("SELECT connector,reference,payload FROM artifact WHERE id=?", (ident,)).fetchone()
    if row is None or row[0] not in {entry["id"] for entry in connectors.load()}:
        return None
    return json.loads(row[1]), json.loads(row[2])


def from_steps(steps: list[dict] | None) -> dict | None:
    if features.preflight("data_artifacts") is not None:
        return None
    for step in reversed(steps or []):
        if not isinstance(step, dict) or step.get("kind") not in ("tool", "tool_result"):
            continue
        output = step.get("output", "")
        if not isinstance(output, str):
            output = json.dumps(output)
        # Parsing picks a reference only. Trusted payloads exist solely in _db.
        for match in RECEIPT_PATTERN.finditer(output.replace('\\"', '"')):
            found = read(match[1])
            if found:
                return found[0]
    return None


def _authorized_artifact(ident: str) -> dict:
    problem = features.preflight("data_artifacts")
    if problem:
        raise HTTPException(status_code=403, detail=problem[1])
    connectors.load(force=True)
    found = read(ident)
    if not found:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return found[1]


@router.get("/api/artifact/analytics/{ident}")
def get_artifact(ident: str):
    from . import chat_store
    payload = _authorized_artifact(ident)
    found = read(ident)
    return JSONResponse({**payload, "reference": found[0] if found else None,
                         "chat_id": chat_store.artifact_chat_id(ident)},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/artifact/analytics/{ident}/chart")
def get_chart(ident: str):
    artifact = _authorized_artifact(ident)
    visualization = artifact.get("visualization")
    if visualization is None or visualization.get('format') != 'svg':
        raise HTTPException(status_code=404, detail="No chart image recorded")
    validate_visualization(visualization, artifact.get("result_id", artifact.get("id")))
    return Response(visualization["content"], media_type="image/svg+xml", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


@router.get("/api/artifact/analytics/{ident}/export")
def export_artifact(ident: str):
    artifact = _authorized_artifact(ident)
    if artifact.get('mode') == 'live':
        # The saved chart definition is reproducible; live query rows are not a snapshot.
        return JSONResponse(artifact, headers={'Cache-Control': 'no-store',
                            'Content-Disposition': 'attachment; filename="live-chart.json"'})
    result = artifact["result"]
    content = io.StringIO(newline="")
    columns = [column["name"] for column in result["columns"]]
    writer = csv.writer(content)
    writer.writerow(columns)
    for row in result["rows"]:
        values = [row.get(key) for key in columns]
        writer.writerow(["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                         else value for value in values])
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.csv", content.getvalue())
        archive.writestr("manifest.json", json.dumps({key: value for key, value in result.items()
                                                    if key != "rows"}, indent=2, ensure_ascii=False))
    return Response(output.getvalue(), media_type="application/zip", headers={
        "Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="analysis-{uuid.UUID(ident)}.zip"',
    })
