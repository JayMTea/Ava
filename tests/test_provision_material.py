import json
from pathlib import Path
from unittest.mock import patch
import pytest
from ava_bridge import audit, provision_material as material


def test_material_roundtrip_and_retirement_are_scoped(tmp_path, monkeypatch):
    ledger = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "_path", lambda: str(ledger))
    with patch.object(material.settings, "agent_state_dir", return_value=str(tmp_path)):
        a = "mcp_server_connectors/apps/demo/demo_find.mjs"
        b = "policies/generated/demo.yaml"
        material.install({a: "export default {};", b: "name: ava-demo"})
        assert material.collect() == {a: "export default {};", b: "name: ava-demo"}
        assert not ledger.exists()
        material.install({}, connector="demo", scopes={"servers"})
        assert material.collect() == {b: "name: ava-demo"}
        event = json.loads(ledger.read_text())
        assert event["kind"] == "connector_prune"
        assert event["id"] == "demo"
        assert event["tool_files"] == [a]
        assert event["policies"] == []
        assert event["reason"] == "stale material removed on remote provision"
        # An already-empty scope produces no extra deletion record.
        recorded = ledger.read_bytes()
        material.install({}, connector="demo", scopes={"servers"})
        assert ledger.read_bytes() == recorded
        material.install({}, connector="demo", scopes={"policies"})
        assert material.collect() == {}
        events = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert len(events) == 2
        assert events[1]["id"] == "demo"
        assert events[1]["policies"] == ["demo"]
        assert events[1]["tool_files"] == []


def test_material_retirement_audits_only_successful_deletions(tmp_path, monkeypatch):
    ledger = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "_path", lambda: str(ledger))
    monkeypatch.setattr(material.settings, "agent_state_dir", lambda: str(tmp_path))
    policy = "policies/generated/demo.yaml"
    tool = "mcp_server_connectors/apps/demo/demo_find.mjs"
    material.install({policy: "name: ava-demo", tool: "export default {};"})
    unlink = Path.unlink

    def refuse_tool(path, *args, **kwargs):
        if path == tmp_path / tool:
            raise PermissionError("test deletion failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_tool)
    with pytest.raises(PermissionError, match="test deletion failure"):
        material.install({}, connector="demo")

    assert not (tmp_path / policy).exists()
    assert (tmp_path / tool).exists()
    event = json.loads(ledger.read_text())
    assert event["kind"] == "connector_prune"
    assert event["policies"] == ["demo"]
    assert event["tool_files"] == []


@pytest.mark.parametrize("name", ["../../etc/passwd", "/tmp/anything", "policies/generated/../bad.yaml",
                                 "mcp_server_connectors/_server.mjs", "connectors/demo/connector.yaml"])
def test_host_code_and_traversal_cannot_cross_the_material_boundary(name, tmp_path):
    with patch.object(material.settings, "agent_state_dir", return_value=str(tmp_path)):
        with pytest.raises(ValueError):
            material.install({name: "bad"})
        assert list(tmp_path.iterdir()) == []


def test_material_size_is_bounded():
    with pytest.raises(ValueError):
        material.validate({"policies/generated/demo.yaml": "x" * material.MAX_BYTES})
