"""The first-run walkthrough remembers what it has shown, per page.

State lives in ava.yaml rather than the browser for a specific reason: Ava is
single-user but MULTI-DEVICE (an installable PWA), so a localStorage flag would
replay the whole walkthrough on the owner's phone and after any cache clear —
which is the returning-user case the feature exists to suppress.

Per page, not one boolean, because Skip dismisses the page in front of you: a
user who never opens Data must not have its walkthrough consumed on their behalf.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-tour-"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava_bridge import hub_api, settings
from ava_bridge.hub import tour


@pytest.fixture
def client(tmp_path, monkeypatch):
    saved: dict = {}

    def fake_patch(patch):
        # Mirror save_patch's deep-merge shape without touching the real yaml.
        saved.setdefault("ui", {}).update(patch.get("ui", {}))

    monkeypatch.setattr(settings, "save_patch", fake_patch)
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None:
                        saved.get("ui", {}).get("tours_seen", default)
                        if key == "ui.tours_seen" else default)
    app = FastAPI()
    app.include_router(hub_api.router)
    return TestClient(app)


def test_a_fresh_install_has_seen_nothing(client) -> None:
    """Absent key means unseen, deliberately. The mirror default — absent means
    seen — fails by silently denying the walkthrough to the people who have been
    using an unexplained UI the longest."""
    r = client.get("/api/hub/tour")
    assert r.status_code == 200
    assert r.json()["seen"] == []
    assert set(r.json()["pages"]) == set(tour.PAGES)


def test_finishing_one_page_does_not_consume_the_others(client) -> None:
    """The whole reason this is a list. Marking Data seen because someone
    finished Setup would spend a walkthrough they never saw."""
    client.post("/api/hub/tour/seen", params={"page": "vitals"})
    assert client.get("/api/hub/tour").json()["seen"] == ["vitals"]


def test_marking_seen_twice_is_not_an_error(client) -> None:
    client.post("/api/hub/tour/seen", params={"page": "ops"})
    r = client.post("/api/hub/tour/seen", params={"page": "ops"})
    assert r.status_code == 200
    assert r.json()["seen"] == ["ops"]


def test_an_unknown_page_is_refused_and_writes_nothing(client) -> None:
    """settings.save_patch takes ANY nested key — there is no schema to stop it —
    so this allowlist is the schema. Without it a typo writes junk into ava.yaml
    that nothing reads and nothing cleans up."""
    r = client.post("/api/hub/tour/seen", params={"page": "../../etc"})
    assert r.status_code == 400
    assert client.get("/api/hub/tour").json()["seen"] == []


def test_reset_clears_every_page(client) -> None:
    """The Replay control in Setup -> System."""
    client.post("/api/hub/tour/seen", params={"page": "hub"})
    client.post("/api/hub/tour/seen", params={"page": "chat"})
    assert client.post("/api/hub/tour/reset").json()["seen"] == []
    assert client.get("/api/hub/tour").json()["seen"] == []


def test_a_write_is_visible_without_a_restart(client) -> None:
    """settings.py loads config once at import, so a value snapshotted into a
    config.py constant would need a bridge restart before a dismissal took
    effect — and "I clicked Skip and it came back" is the one bug this route
    cannot afford. seen() must read through settings at request time."""
    client.post("/api/hub/tour/seen", params={"page": "data"})
    assert "data" in tour.seen()


def test_garbage_in_the_config_does_not_crash_the_route(client, monkeypatch) -> None:
    """A hand-edited ava.yaml is not a supported workflow, but it must not take
    the shell down — the walkthrough is the least important thing on the page."""
    monkeypatch.setattr(settings, "get", lambda key, default=None: "not-a-list")
    assert tour.seen() == []
    assert client.get("/api/hub/tour").status_code == 200
