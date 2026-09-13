"""Product contracts: independent owners, portable state, explicit adapters."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import numpy as np
import pytest

from ava_bridge import settings


def test_explicit_instance_does_not_read_checkout_environment(tmp_path):
    code, home = tmp_path / "code", tmp_path / "instance"
    (code / "ava_bridge").mkdir(parents=True)
    home.mkdir()
    source = Path(settings.__file__).read_text(encoding="utf-8")
    module = code / "ava_bridge" / "settings.py"
    module.write_text(source, encoding="utf-8")
    (code / ".env").write_text("AVA_OWNER_NAME=checkout-owner\nPRIVATE_APP_TOKEN=private-value\n")
    (home / ".env").write_text("AVA_OWNER_NAME=instance-owner\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AVA_", "PRIVATE_APP_"))}
    env["AVA_HOME"] = str(home)
    check = "import runpy,sys,os; s=runpy.run_path(sys.argv[1]); print(s['owner_name']()); print('PRIVATE_APP_TOKEN' in os.environ)"
    got = subprocess.run([sys.executable, "-c", check, str(module)], env=env,
                         capture_output=True, text=True, check=True)
    assert got.stdout.splitlines() == ["instance-owner", "False"]


def test_second_instance_cannot_adopt_or_delete_checkout_voice(tmp_path, monkeypatch):
    import speaker
    legacy = tmp_path / "legacy.npy"
    own = tmp_path / "own.npy"
    np.save(legacy, np.array([1., 0.]))
    monkeypatch.setattr(speaker, "VOICEPRINT", str(own))
    monkeypatch.setattr(speaker, "_LEGACY_VOICEPRINT", str(legacy))
    monkeypatch.setattr(speaker, "_legacy_enabled", lambda: False)
    assert speaker.load_voiceprint() is None
    assert not own.exists()
    speaker.delete_voiceprint()
    assert legacy.is_file()
    assert speaker.voiceprint_paths() == [str(own)]


def test_backup_restore_checksums_and_new_home_only(tmp_path, monkeypatch):
    from ava_bridge import instance
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(settings, "AVA_HOME", home)
    monkeypatch.setattr(settings, "_CFG", {})
    monkeypatch.setattr(settings, "data_dir", lambda: str(home / "custom-data"))
    (home / "custom-data").mkdir()
    import sqlite3
    with sqlite3.connect(home / "custom-data" / "sample.db") as db:
        db.execute("CREATE TABLE example (value TEXT)")
        db.execute("INSERT INTO example VALUES ('keep my work')")
    (home / "ava.yaml").write_text("owner: {name: Example}\npaths: {data: /previous/location}\n")
    (home / ".env").write_text("AVA_SECRET=source-instance-key\nAVA_OWNER_NAME=Example\n")
    archive = tmp_path / "backup.zip"
    instance.backup(str(archive))
    restored = tmp_path / "restored"
    instance.restore(str(archive), str(restored))
    with sqlite3.connect(restored / "data" / "sample.db") as db:
        assert db.execute("SELECT value FROM example").fetchone()[0] == "keep my work"
    assert "/previous/location" not in (restored / "ava.yaml").read_text()
    assert "AVA_SECRET=" not in (restored / ".env").read_text()
    assert "AVA_OWNER_NAME=Example" in (restored / ".env").read_text()
    with pytest.raises(ValueError, match="new directory"):
        instance.restore(str(archive), str(home))


def test_general_chart_needs_no_application_specific_fields(tmp_path, monkeypatch):
    from ava_bridge import data_artifacts, data_api
    monkeypatch.setattr(settings, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(data_artifacts.features, "preflight", lambda key: None)
    monkeypatch.setattr(data_artifacts.connectors, "load", lambda **kw: [{"id": "warehouse"}])
    monkeypatch.setattr(data_artifacts.connectors, "apps", lambda: [])
    ident = str(uuid.uuid4())
    artifact = {"schema_version": "ava-artifact/3", "id": ident, "type": "analytics",
                "mode": "snapshot", "title": "Orders", "chart_type": "bar", "result": {
                    "schema_version": "analysis-result/2", "id": ident, "mode": "snapshot",
                    "title": "Orders", "dimension_label": "Department", "unit": "orders",
                    "created_at": "2026-09-13T12:00:00Z", "method": "Count",
                    "rows": [{"label": "Sales", "value": 12}], "row_count": 1,
                    "columns": [{"name": "label"}, {"name": "value"}],
                    "sources": [], "citations": [], "limitations": []}}
    receipt = data_artifacts.capture("warehouse", {"_meta": {"ava/artifact": artifact}})
    stored = receipt["structuredContent"]["ava_artifact_id"]
    assert data_artifacts.read(stored)[1]["result"] == artifact["result"]
    assert Path(data_artifacts.db_path()).parent == tmp_path
    assert data_artifacts.inventory()["rows"] == 1
    assert data_api.delete_store("artifacts")["rows"] == 1
    assert data_artifacts.read(stored) is None


def test_live_chart_path_is_declared_by_operator(monkeypatch):
    from ava_bridge.artifact_contracts import validate
    from ava_bridge import connectors
    monkeypatch.setattr(connectors, "load", lambda: [{"id": "orders", "x_artifacts": {"live_paths": ["/charts"]}}])
    artifact = {"schema_version": "ava-artifact/3", "id": str(uuid.uuid4()), "type": "analytics",
                "mode": "live", "title": "Orders", "chart": {"citations": []},
                "visualization": {"format": "app", "path": "/charts/saved-7?range=week"}}
    validate(artifact, "orders")
    for path in ("/admin", "//evil.example/chart", "/charts/%2e%2e/admin", "/charts/../admin", "/charts\\admin"):
        artifact["visualization"]["path"] = path
        with pytest.raises(ValueError):
            validate(artifact, "orders")


def test_password_verifier_accepts_legacy_and_never_stores_new_password(tmp_path, monkeypatch):
    from ava_bridge import auth
    path = tmp_path / "password"
    monkeypatch.setattr(auth, "_PASSWORD_FILE", str(path))
    monkeypatch.delenv("AVA_PASSWORD", raising=False)
    path.write_text("legacy-password")
    assert auth.verify_password("legacy-password")
    auth.set_password("a-new-private-password")
    assert "a-new-private-password" not in path.read_text()
    assert auth.verify_password("a-new-private-password")
    assert not auth.verify_password("wrong-password")


def test_installed_runtime_needs_no_core_registry_edit(tmp_path, monkeypatch):
    from ava_bridge import runtime, config, provision
    monkeypatch.setattr(settings, "AVA_HOME", tmp_path)
    monkeypatch.setattr(config, "AGENT_RUNTIME", "custom-test")
    folder = tmp_path / "runtime_adapters" / "custom-test"
    folder.mkdir(parents=True)
    (folder / "extension.yaml").write_text("api_version: ava-extension/1\nruntime: runtime.py:Runtime\n")
    (folder / "runtime.py").write_text('''from ava_bridge.runtime.base import AgentRuntime
class Runtime(AgentRuntime):
    name = "custom-test"
    def available(self): return True
    def run_turn(self, text, session_id=None, history=None): return text.upper(), []
''')
    runtime._INSTALLED.pop("custom-test", None)
    adapter = runtime.configured()
    assert adapter.run_turn("hello") == ("HELLO", [])
    assert runtime.name_error() is None
    assert provision.observed(adapter)["record"] is None
    assert provision.desired() == {s: [] for s in provision.SCOPES}
    runtime._INSTALLED.pop("custom-test", None)


def test_restore_refuses_tampering_before_creating_home(tmp_path):
    from ava_bridge import instance
    import zipfile
    archive = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("data/file", "tampered")
        z.writestr("instance-manifest.json", json.dumps({"format": instance.FORMAT,
                   "files": {"data/file": {"bytes": 8, "sha256": "not-the-checksum"}}}))
    with pytest.raises(ValueError, match="checksum"):
        instance.restore(str(archive), str(tmp_path / "target"))
    assert not (tmp_path / "target").exists()


def test_adopting_enrollment_preserves_source_and_refuses_replacement(tmp_path, monkeypatch):
    from ava_bridge import instance
    source = tmp_path / "previous.npy"
    np.save(source, np.ones(192, dtype=np.float32))
    original = source.read_bytes()
    monkeypatch.setattr(settings, "models_dir", lambda: str(tmp_path / "own-models"))
    result = instance.adopt(str(source), "adopt-voiceprint")
    assert result["source_preserved"]
    assert Path(result["path"]).read_bytes() == original == source.read_bytes()
    with pytest.raises(ValueError, match="already has"):
        instance.adopt(str(source), "adopt-voiceprint")
    assert source.read_bytes() == original


def test_distinct_home_does_not_inherit_untracked_checkout_connector(tmp_path, monkeypatch):
    from ava_bridge import connectors
    builtin, own = tmp_path / "source-connectors", tmp_path / "instance-connectors"
    builtin.mkdir()
    own.mkdir()
    (builtin / "builtins.json").write_text('["product"]')
    for folder, label in ((builtin / "product", "Product"),
                          (builtin / "private-owner-app", "Must not load"),
                          (own / "my-app", "My app")):
        folder.mkdir()
        (folder / "connector.yaml").write_text(f"label: {label}\n")
    monkeypatch.setattr(connectors, "BUILTIN_DIR", str(builtin))
    monkeypatch.setattr(connectors, "USER_DIR", str(own))
    assert {entry["id"] for entry in connectors.catalog()} == {"product", "my-app"}


def test_newer_artifact_database_is_not_downgraded(tmp_path, monkeypatch):
    import sqlite3
    from ava_bridge import data_artifacts
    monkeypatch.setattr(settings, "data_dir", lambda: str(tmp_path))
    with sqlite3.connect(data_artifacts.db_path()) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="newer Ava"):
        data_artifacts.delete_all()
    with sqlite3.connect(data_artifacts.db_path()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
