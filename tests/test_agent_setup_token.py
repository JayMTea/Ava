"""Malformed setup requests must never erase or replace gateway credentials."""
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava_bridge.hub import agent


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(agent.router)
    with mock.patch.object(agent.settings, "write_secret") as write, \
         mock.patch.object(agent.settings, "clear_secret") as clear, \
         mock.patch.object(agent.settings, "env_override", return_value=None), \
         mock.patch.object(agent.runtime, "configured") as runtime:
        yield TestClient(app), write, clear, runtime.return_value.control_plane.return_value


@pytest.mark.parametrize("payload", [{}, [], None, {"token": False}, {"token": 123},
                                    {"token": {}}, {"token": "a\nb"}, {"token": "x" * 8193}])
def test_invalid_input_never_mutates_secret(client, payload):
    http, write, clear, gateway = client
    response = http.post("/agent/gateway/token", json=payload)
    assert response.status_code == 400
    write.assert_not_called()
    clear.assert_not_called()
    gateway.reconnect.assert_not_called()


def test_malformed_json_does_not_clear_secret(client):
    http, write, clear, _ = client
    assert http.post("/agent/gateway/token", content="{").status_code == 400
    write.assert_not_called()
    clear.assert_not_called()


def test_save_never_returns_credential_and_reconnects(client):
    http, write, clear, gateway = client
    response = http.post("/agent/gateway/token", json={"token": "test-credential"})
    assert response.status_code == 200
    assert "test-credential" not in response.text
    write.assert_called_once_with("openclaw_gateway_token", "test-credential")
    clear.assert_not_called()
    gateway.reconnect.assert_called_once()


def test_explicit_clear_also_reconnects(client):
    http, write, clear, gateway = client
    response = http.post("/agent/gateway/token", json={"token": ""})
    assert response.json()["configured"] is False
    clear.assert_called_once_with("openclaw_gateway_token")
    write.assert_not_called()
    gateway.reconnect.assert_called_once()


def test_environment_override_is_not_reported_as_saved(client):
    http, write, clear, _ = client
    with mock.patch.object(agent.settings, "env_override", return_value="AVA_OC_GATEWAY_TOKEN"):
        assert http.post("/agent/gateway/token", json={"token": "new-value"}).status_code == 409
    write.assert_not_called()
    clear.assert_not_called()
