"""Readers for the original analytics and Superset contracts. Kept for saved results."""
import json
import math
import re
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 4 * 1024 * 1024


def validate(artifact: dict) -> None:
    from .data_artifacts import validate_visualization
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
        validate_visualization(visualization, artifact["result_id"])


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
    if not isinstance(chart.get('sources_in_view', False), bool):
        raise ValueError('Invalid chart sources_in_view')
    if len(json.dumps(artifact, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError('Artifact exceeds the snapshot budget')


