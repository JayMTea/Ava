"""Capture typed artifacts from authenticated connector results, never model HTML.

The model receives a compact reference. The full immutable snapshot is served
through Ava's existing authenticated browser boundary and survives chat reloads.
"""

import csv
import contextlib
import io
import json
import math
import os
import re
import sqlite3
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from . import connectors, features, settings

router = APIRouter()
MAX_BYTES = 4 * 1024 * 1024
RECEIPT_PATTERN = re.compile(r'"ava_artifact_id"\s*:\s*"([a-f0-9-]{36})"')


@contextlib.contextmanager
def _db():
    path = settings.home("data", "analytics-artifacts.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("CREATE TABLE IF NOT EXISTS artifact "
                       "(id TEXT PRIMARY KEY, connector TEXT NOT NULL, created REAL NOT NULL, "
                       "reference TEXT NOT NULL, payload TEXT NOT NULL)")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _validate(artifact: dict) -> None:
    if artifact.get('schema_version') == 'ava-artifact/2':
        _validate_superset(artifact)
        return
    if artifact.get("schema_version") != "ava-artifact/1" or artifact.get("type") != "analytics":
        raise ValueError("Unsupported artifact contract")
    result = artifact.get("result")
    if not isinstance(result, dict) or result.get("schema_version") != "analysis-result/1":
        raise ValueError("Artifact has no recorded analytical result")
    if str(uuid.UUID(str(result.get("id")))) != artifact.get("result_id"):
        raise ValueError("Artifact and result identities differ")
    if artifact.get("mode") != "snapshot" or result.get("mode") != "snapshot":
        raise ValueError("Only recorded snapshots can use the analytical renderer")
    if artifact.get("chart_type", "bar") not in ("bar", "table"):
        raise ValueError("Unsupported chart type")
    for key in ("title", "created_at", "unit", "method", "metric_id", "metric_version"):
        if not isinstance(result.get(key), str) or len(result[key]) > 65536:
            raise ValueError("Invalid analytical description")
    datetime.fromisoformat(result["created_at"])
    filters = result.get("filters")
    if not isinstance(filters, dict) or not all(isinstance(filters.get(key), str) for key in (
        "release", "record_type", "geography_level",
    )) or not isinstance(filters.get("states"), list):
        raise ValueError("Invalid analytical scope")
    for key, fields in (("sources", ("dataset_id", "url", "source_sha256", "silver_sha256", "published_object")),
                        ("citations", ("title", "url")), ("columns", ("name",))):
        entries = result.get(key)
        if not isinstance(entries, list) or len(entries) > 10000 or any(
            not isinstance(entry, dict) or not all(isinstance(entry.get(field), str) for field in fields)
            for entry in entries
        ):
            raise ValueError("Invalid analytical evidence")
    if not isinstance(result.get("limitations"), list) or not all(
        isinstance(item, str) for item in result["limitations"]
    ):
        raise ValueError("Invalid analytical limitations")
    rows = result.get("rows")
    if not isinstance(rows, list) or len(rows) > 5000 or result.get("row_count") != len(rows):
        raise ValueError("Invalid artifact row count")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            raise ValueError("Invalid analytical row")
        for key in ("value", "moe_90", "sample_records"):
            value = row.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise ValueError("Invalid analytical value")
    if len(json.dumps(artifact, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError("Artifact exceeds the snapshot budget")
    visualization = artifact.get("visualization")
    if visualization is not None:
        _validate_visualization(visualization, artifact["result_id"])


def _validate_superset(artifact: dict) -> None:
    """A native chart runs only within its configured connector's app boundary."""
    if artifact.get('type') != 'analytics' or artifact.get('mode') != 'live':
        raise ValueError('Invalid native chart contract')
    uuid.UUID(str(artifact.get('id')))
    chart, visual = artifact.get('chart'), artifact.get('visualization')
    if not isinstance(chart, dict) or not isinstance(visual, dict) or visual.get('format') != 'superset':
        raise ValueError('Invalid Superset chart')
    if type(chart.get('id')) is not int or chart['id'] <= 0:
        raise ValueError('Invalid saved chart ID')
    if not isinstance(artifact.get('title'), str) or not 1 <= len(artifact['title']) <= 1000:
        raise ValueError('Invalid chart title')
    chart_type = artifact.get('chart_type')
    if not isinstance(chart_type, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', chart_type):
        raise ValueError('Invalid Superset chart type')
    if chart.get('chart_type') != chart_type:
        raise ValueError('Chart types differ')
    path = visual.get('path')
    if not isinstance(path, str) or len(path) > 64000:
        raise ValueError('Invalid chart destination')
    url = urlsplit(path)
    params = parse_qs(url.query, keep_blank_values=True)
    if (url.scheme or url.netloc or url.fragment or url.path != '/superset/superset/explore/'
            or set(params) != {'slice_id', 'standalone', 'form_data'}
            or any(len(values) != 1 for values in params.values())
            or params['slice_id'] != [str(chart['id'])] or params['standalone'] != ['1']):
        raise ValueError('Invalid chart destination')
    form = json.loads(params['form_data'][0])
    if (not isinstance(form, dict) or set(form) != {'slice_id', 'adhoc_filters'}
            or form['slice_id'] != chart['id'] or not isinstance(form['adhoc_filters'], list)
            or form['adhoc_filters'] != chart.get('filters')):
        raise ValueError('Chart destination and saved scope differ')
    citations = chart.get('citations')
    if not isinstance(citations, list) or len(citations) > 100 or any(
        not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('title', 'url'))
        for item in citations
    ):
        raise ValueError('Invalid chart citations')
    if len(json.dumps(artifact, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError('Artifact exceeds the snapshot budget')


def _validate_visualization(visualization, result_id: str) -> None:
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
        _validate(artifact)
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
    _validate_visualization(visualization, artifact["result_id"])
    return Response(visualization["content"], media_type="image/svg+xml", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


@router.get("/api/artifact/analytics/{ident}/export")
def export_artifact(ident: str):
    artifact = _authorized_artifact(ident)
    if artifact.get('schema_version') == 'ava-artifact/2':
        # The saved chart definition is reproducible; live query rows are not a snapshot.
        return JSONResponse(artifact, headers={'Cache-Control': 'no-store',
                            'Content-Disposition': 'attachment; filename="superset-chart.json"'})
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
