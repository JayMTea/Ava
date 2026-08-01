"""The server-rendered brand surfaces: /login, /setup, /setup/wizard, assets.

These are the pages a re-branded install is most visible on and the ones the
SPA's design system does not reach, so they are covered here rather than left to
the browser tier.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from ava_bridge import brand, settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    import phone_bridge
    importlib.reload(phone_bridge)
    # base_url matters: the DNS-rebinding Host allowlist (auth.host_is_trusted)
    # answers 421 for TestClient's default `testserver`. Same convention as
    # tests/test_auth.py and tests/test_password_change.py.
    return TestClient(phone_bridge.app, base_url="http://localhost")


@pytest.fixture()
def authed(tmp_path, monkeypatch):
    """A client holding a session cookie, for the routes behind the auth gate."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setenv("AVA_PASSWORD", "brandtest-password")
    import phone_bridge
    importlib.reload(phone_bridge)
    c = TestClient(phone_bridge.app, base_url="http://localhost")
    r = c.post("/login", data={"password": "brandtest-password"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    return c


def _set(monkeypatch, **cfg):
    """Point the live config at a brand block without touching disk."""
    merged = {"brand": cfg}
    monkeypatch.setattr(settings, "_CFG", merged, raising=False)


# ---- The bug the old `.replace("Ava", …)` would have shipped ---------------
def test_login_page_does_not_corrupt_words_that_merely_start_with_ava(monkeypatch):
    """The regression the sentinel switch exists to prevent.

    `LOGIN_PAGE = _f.read().replace("Ava", config.AVA_NAME)` was a blanket
    substring replace over the whole document. It worked only because login.html
    happened to contain no other word starting "Ava" — the first "Available" in
    that page would have had the corporate name spliced through it. Assert the
    property directly rather than trusting that nobody writes the word.
    """
    _set(monkeypatch, name="Northwind")
    tmpl = ('<title>__BRAND__</title><p>No slots are Available right now.</p>'
            '<p>Your Avatar is set.</p>')
    out = brand.render_page(tmpl)
    assert "<title>Northwind</title>" in out
    assert "Available" in out, "a substring replace would have mangled this"
    assert "Avatar" in out
    assert "Northwindilable" not in out


def test_the_templates_carry_no_bare_ava_left_for_a_substring_replace():
    """All three pages are sentinel-driven now, so there is nothing for a future
    `.replace("Ava", …)` to be reintroduced for."""
    import os

    from ava_bridge import config
    for f in ("login.html", "setup.html", "setup_wizard.html", "claim.html"):
        with open(os.path.join(config.WEB_DIR, f), encoding="utf-8") as fh:
            body = fh.read()
        assert "Ava" not in body, f"{f} still hardcodes the brand name"
        assert "__BRAND__" in body
        assert "__BRAND_CSS__" in body, f"{f} still carries its own token block"


def test_the_three_pages_no_longer_each_own_a_token_block():
    """login/setup/setup_wizard carried three byte-identical `:root` blocks, all
    hardcoding #007acc — so a re-branded install had a blue sign-in page."""
    import os

    from ava_bridge import config
    for f in ("login.html", "setup.html", "setup_wizard.html", "claim.html"):
        with open(os.path.join(config.WEB_DIR, f), encoding="utf-8") as fh:
            body = fh.read()
        assert "#007acc" not in body, f"{f} still hardcodes the shipped accent"


# ---- Default stays default -------------------------------------------------
def test_unbranded_css_is_the_shipped_palette_byte_for_byte(monkeypatch):
    """The whole "default look is unchanged" promise, asserted on the bytes."""
    _set(monkeypatch)
    css = brand.pre_auth_css()
    assert "--accent:#007acc" in css
    assert "--accent-hover:#0068ad" in css
    assert "--bg:#262624" in css


def test_a_branded_install_reaches_the_sign_in_page(monkeypatch):
    _set(monkeypatch, accent="#8b1a3d", chrome="#120a0d", name="Northwind")
    css = brand.pre_auth_css()
    assert "--accent:#8b1a3d" in css
    assert "--bg:#120a0d" in css
    assert "#007acc" not in css
    # hover is derived, not stored, and matches the CSS color-mix 86% figure
    assert "--accent-hover:#781634" in css   # 0x8b1a3d x 0.86, rounded


def test_brand_public_false_renders_avas_own_default_not_a_blank(monkeypatch):
    """A blank card announces that something is hidden, which is worse than the
    default it conceals."""
    _set(monkeypatch, name="Northwind", accent="#8b1a3d", public=False)
    assert brand.pre_auth_name() == "Ava"
    assert "#007acc" in brand.pre_auth_css()
    assert brand.pre_auth_mark() == brand._SHIPPED_MARK


# ---- Escaping --------------------------------------------------------------
def test_the_brand_name_is_escaped_into_the_page(monkeypatch):
    """Owner-controlled today; third-party-controlled the moment a brand pack
    can set it, and it lands inside a <title> and an <h1>."""
    _set(monkeypatch, name='<script>alert(1)</script>')
    out = brand.render_page("<title>__BRAND__</title>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ---- The public health endpoint no longer names the instance ---------------
def test_health_does_not_leak_the_brand(client):
    """/api/health is public. It returned `brand` with ZERO consumers — api.ts
    does not declare it, the qa contract requires only ["ok"], no fixture reads
    it. A pre-auth disclosure with no caller is not a feature."""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert "brand" not in r.json()
    assert r.json()["ok"] is True


def test_api_brand_still_answers_the_contract_and_is_still_gated(client):
    """qa/test_02_api_contracts requires `name`; adding fields is safe, and the
    endpoint must NOT have become public along the way."""
    r = client.get("/api/brand")
    assert r.status_code == 401, "must stay cookie-gated"


# ---- The asset route -------------------------------------------------------
def test_asset_slot_is_an_enum_not_a_filename(client):
    """No traversal defence is needed because there is no caller-supplied path.

    The property asserted is "nothing is ever served", not a specific status.
    `../../etc/passwd` is normalised by the HTTP client before it leaves, so it
    arrives as /etc/passwd and the auth gate rejects it with 403 — it never
    reaches this handler at all. Pinning 404 would be asserting an accident of
    which layer said no first; what matters is that no bytes come back.
    """
    for bad in ("../../etc/passwd", "..", "logo.png", "nope", "%2e%2e%2fetc"):
        r = client.get(f"/brand/asset/{bad}")
        assert r.status_code in (403, 404), (bad, r.status_code)
        assert not r.headers.get("content-type", "").startswith("image/"), bad


def test_every_slot_in_the_enum_is_routable_and_no_other_name_is():
    """The enum is the whole allowlist, so it is worth stating as one."""
    assert set(brand.SLOTS) == {"logo", "logo_light", "wordmark",
                                "wordmark_light", "icon"}
    assert brand.asset_name("../../etc/passwd") == ""
    assert brand.asset_name("nope") == ""


def test_asset_slots_are_public_so_the_sign_in_page_can_show_a_logo():
    from ava_bridge.auth import _PUBLIC_PATHS
    for slot in brand.SLOTS:
        assert f"/brand/asset/{slot}" in _PUBLIC_PATHS


def test_unset_slot_404s_rather_than_serving_a_placeholder(client):
    """A fallback image makes "did my upload work?" unanswerable."""
    assert client.get("/brand/asset/logo").status_code == 404


# ---- The switch that lets a control plane hand out an unbrandable instance ---
def test_branding_writes_are_refused_when_the_switch_is_off(authed, monkeypatch):
    """features.REGISTRY carries an `env` key on every entry precisely so a
    control plane can pin a flag from OUTSIDE the container. Pinning
    AVA_BRANDING=0 is the whole admin-only mechanism — no fleet concept enters
    this repo."""
    monkeypatch.setenv("AVA_BRANDING", "0")
    r = authed.post("/api/hub/branding", json={"name": "Mine"})
    assert r.status_code == 403
    assert r.json()["error_code"] == "branding_off"


def test_the_refusal_does_not_send_a_managed_user_to_a_checkbox(authed, monkeypatch):
    """preflight's stock message names Setup → System. That is right when the
    owner flipped the switch themselves and WRONG when an env var is holding it
    off, because env outranks ava.yaml — the checkbox will not stick and the
    user is sent hunting. The message has to distinguish the two."""
    monkeypatch.setenv("AVA_BRANDING", "0")
    msg = authed.post("/api/hub/branding", json={"name": "Mine"}).json()["error"]
    assert "Setup" not in msg, msg
    assert "environment" in msg.lower()


def test_a_pinned_brand_still_renders_with_the_switch_off(monkeypatch):
    """The switch gates WRITES only. An instance whose brand was set from
    outside must still look like that brand — losing the ability to edit is not
    the same as losing the brand."""
    monkeypatch.setenv("AVA_BRANDING", "0")
    monkeypatch.setenv("AVA_NAME", "Northwind")
    assert brand.pre_auth_name() == "Northwind"
    assert "Northwind" in brand.render_page("<title>__BRAND__</title>")
