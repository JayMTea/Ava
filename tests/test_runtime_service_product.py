"""Cross-process protocol shape tested against the independent example server."""
import importlib.util
from pathlib import Path
import threading

import pytest

from ava_bridge import settings
from ava_bridge.runtime.service import ServiceRuntime
from ava_bridge.runtime.errors import GatewayError, GatewayUnsupported


@pytest.fixture
def service(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "examples/runtime-service/server.py"
    spec = importlib.util.spec_from_file_location("example_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.server(0, "test-runtime-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AVA_RUNTIME_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("AVA_RUNTIME_TOKEN", "test-runtime-token")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_service_calls_an_independent_tool_and_erases_stateless_session(service):
    runtime = ServiceRuntime()
    assert runtime.available()
    assert runtime.supports_tools
    assert runtime.run_turn("sum 12 8 5", "own-session", []) == ("25", ["sum_numbers"])
    assert runtime.discard_session("own-session")
    assert runtime.status()["protocol"] == "ava-runtime/1"
    with pytest.raises(GatewayUnsupported):
        runtime.exec("must never execute")
    with pytest.raises(GatewayUnsupported):
        runtime.provision()


def test_service_authentication_failure_is_not_a_success_or_direct_answer(service, monkeypatch):
    monkeypatch.setenv("AVA_RUNTIME_TOKEN", "wrong-token")
    runtime = ServiceRuntime()
    assert not runtime.available()
    with pytest.raises(GatewayError, match="unavailable"):
        runtime.run_turn("sum 1 2")


def test_service_rejects_redirect_credentials_and_remote_cleartext(monkeypatch):
    monkeypatch.setattr(settings, "_CFG", {})
    monkeypatch.delenv("AVA_RUNTIME_ALLOW_HTTP", raising=False)
    for url in ("http://agent.example.org", "https://user:password@agent.example.org", "https://agent.example.org?key=secret"):
        monkeypatch.setenv("AVA_RUNTIME_URL", url)
        runtime = ServiceRuntime()
        assert not runtime.available()
        with pytest.raises(ValueError):
            runtime._base()


def test_extension_failure_is_visible_and_does_not_hide_login(tmp_path, monkeypatch):
    from ava_bridge import extensions
    from fastapi import FastAPI
    monkeypatch.setattr(settings, "AVA_HOME", tmp_path)
    monkeypatch.setattr(settings, "_CFG", {"extensions": {"enabled": ["broken-example"]}})
    monkeypatch.delenv("AVA_LEGACY_ROUTES", raising=False)
    folder = tmp_path / "extensions/broken-example"
    folder.mkdir(parents=True)
    (folder / "extension.yaml").write_text("api_version: ava-extension/1\nbackend: backend.py:register\n")
    (folder / "backend.py").write_text("def register(app): raise ValueError('secret-value-must-not-be-exposed')\n")
    extensions.mount(FastAPI())
    status = extensions.status()
    assert status["errors"][0]["id"] == "broken-example"
    assert "secret-value" not in str(status)
    extensions._ERRORS.clear()
