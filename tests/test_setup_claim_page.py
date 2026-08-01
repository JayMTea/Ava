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

import re
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


# ---- Completing first run, end to end --------------------------------------
# These deliberately do NOT mock may_claim. The tests above assert what each
# branch renders; these assert that the branches JOIN UP into a first run that
# finishes. That distinction is the whole reason the bug below shipped: every
# test in the repo either mocked the gate or checked a GET, and none had ever
# posted a valid claim, so a POST that the gate refused looked like nobody's
# business. Starlette's TestClient reports its peer as the literal "testclient",
# which _is_loopback() fails closed on -- so an unmodified TestClient IS the
# remote caller this gate is about, which is exactly the Docker first run.
@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A first-run instance with no password and a real, unmocked claim token."""
    from ava_bridge import config
    monkeypatch.setattr(config, "CHATS_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "_CLAIM_FILE", str(tmp_path / "setup_claim"))
    monkeypatch.setattr(auth, "_PASSWORD_FILE", str(tmp_path / "auth_password"))
    monkeypatch.delenv("AVA_PASSWORD", raising=False)
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), auth.claim_token()


def _form_action(body: str) -> str:
    m = re.search(r'<form[^>]*action="([^"]*)"', body)
    assert m, "no form on the page"
    return m.group(1)


def test_a_remote_caller_can_actually_finish_first_run(fresh) -> None:
    """The bug: setup.html posted to a BARE /setup, and may_claim() reads the
    claim only from the query string or the x-ava-setup-claim header. So the
    token that earned you the form was dropped on submit, and the password was
    never set -- for every Docker install, since the container sees the bridge
    gateway and never 127.0.0.1. Open link, type password, 403."""
    client, token = fresh
    page = client.get("/setup", params={"claim": token})
    assert page.status_code == 200

    action = _form_action(page.text)
    assert token in action, (
        f"the password form posts to {action!r}, which does not carry the claim "
        "-- submitting it cannot pass may_claim(), so first run cannot complete")

    done = client.post(action, data={"password": "hunter2hunter2",
                                     "confirm": "hunter2hunter2"},
                       follow_redirects=False)
    assert done.status_code == 303, (
        f"submitting the form the server itself rendered was refused "
        f"({done.status_code}) -- first run is impossible for a remote caller")
    assert auth.current_password() == "hunter2hunter2"


def test_a_recoverable_typo_stays_recoverable(fresh) -> None:
    """The re-render after a validation error must keep the claim too. Dropping
    it there means one mistyped confirm field locks the owner out for good."""
    client, token = fresh
    action = _form_action(client.get("/setup", params={"claim": token}).text)

    again = client.post(action, data={"password": "hunter2hunter2",
                                      "confirm": "hunter2hunter3"})
    assert again.status_code == 400
    assert token in _form_action(again.text), (
        "the retry form dropped the claim, so a mistyped confirmation is fatal")

    short = client.post(action, data={"password": "abc", "confirm": "abc"})
    assert short.status_code == 400
    assert token in _form_action(short.text)


def test_the_token_is_not_handed_to_a_caller_who_never_presented_one(fresh) -> None:
    """A loopback caller passes may_claim() with no token, so the form has no
    reason to carry one. Emitting it anyway would put a live secret in front of
    whoever the relaxed branch let through."""
    client, token = fresh
    with mock.patch.object(pages, "may_claim", lambda request: True):
        body = client.get("/setup").text
    assert token not in body
    assert _form_action(body) == "/setup"


def test_a_hostile_claim_is_never_reflected_into_the_page(fresh) -> None:
    """The action is built from the SERVER's token, never the request's bytes."""
    client, _ = fresh
    hostile = '"><script>alert(1)</script>'
    r = client.get("/setup", params={"claim": hostile})
    assert r.status_code == 403
    assert "<script>alert(1)</script>" not in r.text


# ---- Cross-site writes ------------------------------------------------------
def test_a_cross_site_form_post_cannot_set_the_admin_password(fresh) -> None:
    """POST /setup takes exactly `password` and `confirm`, which makes a
    cross-site auto-submitting form a CORS *simple* request: no preflight, and
    the Host header is this bridge's own so host_is_trusted() passes it.

    may_claim() was never the defence here. It stops this on Docker only as a
    side effect of the peer being the bridge gateway; on bare metal a loopback
    caller passes the gate with no token at all, so any page the owner visited
    during the first-run window could have claimed their instance.
    """
    client, token = fresh
    evil = client.post(f"/setup?claim={token}",
                       data={"password": "attacker-pw-01",
                             "confirm": "attacker-pw-01"},
                       headers={"sec-fetch-site": "cross-site"},
                       follow_redirects=False)
    assert evil.status_code == 403, (
        "a cross-site POST carrying a valid claim set the admin password")
    assert not auth.current_password()


def test_a_mismatched_origin_is_refused_for_clients_without_sec_fetch(fresh) -> None:
    client, token = fresh
    evil = client.post(f"/setup?claim={token}",
                       data={"password": "attacker-pw-01",
                             "confirm": "attacker-pw-01"},
                       headers={"origin": "http://evil.example"},
                       follow_redirects=False)
    assert evil.status_code == 403
    assert not auth.current_password()


def test_the_pages_own_form_still_goes_through(fresh) -> None:
    """What a real browser sends when submitting the form Ava rendered."""
    client, token = fresh
    action = _form_action(client.get("/setup", params={"claim": token}).text)
    done = client.post(action,
                       data={"password": "hunter2hunter2",
                             "confirm": "hunter2hunter2"},
                       headers={"sec-fetch-site": "same-origin",
                                "origin": "http://testserver"},
                       follow_redirects=False)
    assert done.status_code == 303, "the browser's own same-origin submit was refused"
    assert auth.current_password() == "hunter2hunter2"


def test_a_non_browser_client_is_not_locked_out(fresh) -> None:
    """No Sec-Fetch-Site and no Origin means no browser sent this, and a request
    no browser sent cannot be CSRF. curl, the health probes and CI rely on it."""
    client, token = fresh
    done = client.post(f"/setup?claim={token}",
                       data={"password": "hunter2hunter2",
                             "confirm": "hunter2hunter2"},
                       follow_redirects=False)
    assert done.status_code == 303
    assert auth.current_password() == "hunter2hunter2"
