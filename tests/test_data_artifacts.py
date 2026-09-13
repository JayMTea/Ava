"""An artifact is a trusted tool snapshot, never a chart invented in model text."""

import io
import json
import uuid
import zipfile

import pytest

from ava_bridge import data_artifacts as artifacts


@pytest.fixture
def snapshot(monkeypatch):
    monkeypatch.setattr(artifacts.features, "preflight", lambda feature: None)
    monkeypatch.setattr(artifacts.connectors, "load", lambda **kwargs: [{"id": "test-data"}])
    monkeypatch.setattr(artifacts.connectors, "apps", lambda: [{"id": "test-data", "url": "/apps/test-data/"}])
    ident = str(uuid.uuid4())
    return {"schema_version": "ava-artifact/1", "type": "analytics", "result_id": ident,
            "mode": "snapshot", "title": "Population", "chart_type": "bar", "result": {
                "schema_version": "analysis-result/1", "id": ident, "mode": "snapshot",
                "title": "Population", "rows": [{"label": "California", "value": 120.0, "moe_90": None}],
                "row_count": 1, "columns": [{"name": "label"}, {"name": "value"}, {"name": "moe_90"}],
                "sources": [], "citations": [], "filters": {"release": "2024", "record_type": "person", "geography_level": "state", "states": ["06"]}, "limitations": [],
                "created_at": "2026-09-12T00:00:00Z", "unit": "people", "method": "Sum of weights",
                "metric_id": "population", "metric_version": "test/1",
            }}


def test_receipt_survives_cli_and_streaming_history_and_export(snapshot):
    envelope = artifacts.capture("test-data", {"_meta": {"ava/artifact": snapshot},
                                              "structuredContent": {"answer": "120"}})
    assert "_meta" not in envelope
    receipt = envelope["structuredContent"]
    ident = receipt["ava_artifact_id"]
    assert artifacts.read(ident)[1]["result"] == snapshot["result"]
    steps = [{"kind": "tool", "output": json.dumps(envelope)}]
    assert artifacts.from_steps(steps)["id"] == ident
    from ava_bridge.turns import _parse_turn_steps
    session = json.dumps({"type": "message", "message": {"role": "toolResult", "toolName": "analysis",
                                                        "content": envelope["content"]}})
    assert artifacts.from_steps(_parse_turn_steps(session))["id"] == ident
    export = artifacts.export_artifact(ident)
    with zipfile.ZipFile(io.BytesIO(export.body)) as archive:
        assert "California,120.0," in archive.read("data.csv").decode()
        assert json.loads(archive.read("manifest.json"))["id"] == snapshot["result_id"]


def test_model_text_cannot_create_artifact(snapshot):
    assert artifacts.from_steps([{"kind": "text", "output": json.dumps(snapshot)}]) is None
    assert artifacts.from_steps([{"kind": "tool", "output": json.dumps({"ava_artifact_id": str(uuid.uuid4())})}]) is None
    assert artifacts.capture("test-data", {"text": json.dumps(snapshot)}) == {"text": json.dumps(snapshot)}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "120"])
def test_invalid_numbers_cannot_reach_chart(snapshot, value):
    snapshot["result"]["rows"][0]["value"] = value
    with pytest.raises(ValueError):
        artifacts.capture("test-data", {"_meta": {"ava/artifact": snapshot}})


def test_revoked_connector_and_disabled_feature_block_retrieval(snapshot, monkeypatch):
    receipt = artifacts.capture("test-data", {"_meta": {"ava/artifact": snapshot}})
    ident = receipt["structuredContent"]["ava_artifact_id"]
    monkeypatch.setattr(artifacts.connectors, "load", lambda **kwargs: [])
    assert artifacts.read(ident) is None
    monkeypatch.setattr(artifacts.features, "preflight", lambda feature: ("disabled", "Feature disabled"))
    with pytest.raises(artifacts.HTTPException) as problem:
        artifacts.get_artifact(ident)
    assert problem.value.status_code == 403
    result = artifacts.capture("test-data", {"_meta": {"ava/artifact": snapshot}})
    assert "_meta" not in result and "ava_artifact_id" not in result["structuredContent"]


def test_chart_image_is_stored_and_served_without_entering_model_context(snapshot):
    image = {"format": "svg", "result_id": snapshot["result_id"],
             "content": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><text x="0" y="20">California: 120</text></svg>'}
    snapshot["visualization"] = image
    receipt = artifacts.capture("test-data", {"_meta": {"ava/artifact": snapshot},
                                             "structuredContent": {"visualization": image}})
    assert "<svg" not in json.dumps(receipt)
    ident = receipt["structuredContent"]["ava_artifact_id"]
    response = artifacts.get_chart(ident)
    assert response.body.decode() == image["content"]
    assert response.media_type == "image/svg+xml"
    assert "sandbox" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("content", [
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
    '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(https://example.com)"/></svg>',
    '<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>',
])
def test_chart_images_refuse_active_or_external_content(snapshot, content):
    snapshot["visualization"] = {"format": "svg", "result_id": snapshot["result_id"], "content": content}
    with pytest.raises(ValueError):
        artifacts.capture("test-data", {"_meta": {"ava/artifact": snapshot}})


def test_chart_must_belong_to_the_recorded_result(snapshot):
    snapshot["visualization"] = {"format": "svg", "result_id": str(uuid.uuid4()),
                                 "content": '<svg xmlns="http://www.w3.org/2000/svg"/>'}
    with pytest.raises(ValueError, match="identities differ"):
        artifacts.capture("test-data", {"_meta": {"ava/artifact": snapshot}})


def test_saved_chart_link_resolves_its_original_conversation(snapshot):
    from ava_bridge import chat_store
    receipt = artifacts.capture("test-data", {"_meta": {"ava/artifact": snapshot}})["structuredContent"]
    reference = receipt["artifact"]
    chat = chat_store.chat_new()
    chat_store.chat_append(chat["id"], "assistant", "Recorded comparison", artifact=reference)
    body = json.loads(artifacts.get_artifact(reference["id"]).body)
    assert body["chat_id"] == chat["id"]
    chat_store.delete(chat["id"])
    assert json.loads(artifacts.get_artifact(reference["id"]).body)["chat_id"] is None
