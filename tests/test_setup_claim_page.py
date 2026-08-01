"""The claim step is a page of its own, not a red line over an unusable form.

`/setup` served the full password form to callers it was about to refuse, with
the claim instructions as a message above it. Every browser on the Docker path
sees that screen — the container reads the compose bridge gateway as the peer
address, never 127.0.0.1 — so the ordinary first run presented a focused,
complete, submittable form whose submission POST /setup was guaranteed to reject.
A real first-time installer typed a password into it and asked why setup was
asking about a claim, which is the correct question to ask of that screen.

These assert the shape, not the copy: an unclaimable caller gets somewhere to put
a token and nowhere to put a password.
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava_bridge import auth, pages


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


def _unclaimable():
    """No password yet, and this caller cannot claim — the Docker first run."""
    return (mock.patch.object(auth, "needs_setup", lambda: True),
            mock.patch.object(pages, "may_claim", lambda request: False))


def test_an_unclaimable_caller_gets_no_password_form(client: TestClient) -> None:
    a, b = _unclaimable()
    with a, b:
        r = client.get("/setup")
    assert r.status_code == 403
    assert 'name="password"' not in r.text, (
        "/setup served the password form to a caller POST /setup will refuse. "
        "That form is the one action on the page that cannot work.")
    assert 'name="claim"' in r.text, (
        "the claim page must offer somewhere to paste the token — otherwise the "
        "only way forward is hand-editing the URL")


def test_a_bare_first_visit_is_not_dressed_as_an_error(client: TestClient) -> None:
    """Hitting the gate is normal here, so the first screen must not read as a
    failure. Only a token that was tried and rejected earns the error line."""
    a, b = _unclaimable()
    with a, b:
        bare = client.get("/setup")
        tried = client.get("/setup", params={"claim": "not-the-token"})
    assert "not accepted" not in bare.text, (
        "a fresh install's first screen claims something went wrong when nothing "
        "has yet")
    assert "not accepted" in tried.text, (
        "a rejected token looks identical to no token at all, so a mistyped "
        "paste gives the reader no signal")


def test_a_claimable_caller_still_gets_the_password_form(client: TestClient) -> None:
    """The gate is the only thing that changed; passing it must be unaffected."""
    with mock.patch.object(auth, "needs_setup", lambda: True), \
         mock.patch.object(pages, "may_claim", lambda request: True):
        r = client.get("/setup")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert 'name="confirm"' in r.text


def test_the_claim_form_targets_the_url_shape_the_gate_already_accepts(
        client: TestClient) -> None:
    """A GET at /setup with a `claim` field rebuilds exactly the link install.sh
    prints, so the browser path and the printed path stay one code path. A POST
    here would need `may_claim` taught a second way in."""
    a, b = _unclaimable()
    with a, b:
        r = client.get("/setup")
    assert 'method="get"' in r.text.lower()
    assert 'action="/setup"' in r.text
