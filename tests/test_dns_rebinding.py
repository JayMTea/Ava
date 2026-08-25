"""A loopback peer is not proof of a local caller. DNS rebinding produces one.

Measured before the Host allowlist existed:

    may_claim  peer=127.0.0.1  Host=evil.example.com:8096  -> True

So a page the owner merely visited — on someone else's domain, whose DNS TTL
expires and re-resolves to 127.0.0.1 — could set the admin password on an
unclaimed bare-metal install.

**`samesite="lax"` is not a defence here, and that is the subtle part.** After
rebinding, the attacker's page and the bridge are the SAME ORIGIN as far as the
browser is concerned, so the session cookie is sent and every authenticated route
is reachable too. No cookie attribute and no `Origin` check can see the difference.
Only the server can, by noticing it was asked for under a name it does not answer
to — which is why the fix is a Host allowlist and why it sits above every other
check in `auth_gate`.
"""
from __future__ import annotations

import pytest

from ava_bridge import auth


class _Req:
    def __init__(self, host: str, ip: str = "127.0.0.1"):
        self.client = type("C", (), {"host": ip})()
        self.headers = {"host": host}
        self.query_params = {}
        self.scope = {"client": (ip, 1)}


# ---- the attack -------------------------------------------------------------- #
def test_a_rebound_host_is_refused_even_from_a_loopback_peer() -> None:
    ok, why = auth.host_is_trusted(_Req("evil.example.com:8096"))
    assert ok is False
    assert "DNS rebinding" in why, (
        "the refusal does not say what it suspects, so an owner hitting it "
        "legitimately behind a proxy has no idea what to do")
    assert "server.trusted_hosts" in why, "the escape hatch is not named"


def test_may_claim_refuses_a_rebound_host() -> None:
    """/setup sets the admin password; it should not rest on one check."""
    assert auth.may_claim(_Req("localhost:8096")) is True
    assert auth.may_claim(_Req("evil.example.com:8096")) is False, (
        "a rebound page could still claim an unclaimed instance. The gate refuses "
        "it too, but this is the route that matters most and belt-and-braces here "
        "is cheap.")


def test_a_suffix_lookalike_does_not_pass() -> None:
    """`127.0.0.1.evil.com` contains a trusted name as a prefix."""
    assert auth.host_is_trusted(_Req("127.0.0.1.evil.com"))[0] is False
    assert auth.host_is_trusted(_Req("localhost.evil.com"))[0] is False


# ---- the shapes a real client sends ------------------------------------------ #
@pytest.mark.parametrize("host,want", [
    ("localhost", True),
    ("localhost:8096", True),
    ("127.0.0.1", True),
    ("127.0.0.1:8096", True),
    ("::1", True),
    ("[::1]", True),
    ("[::1]:8096", True),          # the bracketed form my first cut got wrong
    ("host.docker.internal:8096", True),
    ("host.openshell.internal:8096", True),   # the agent sandbox's name for this host
    ("host.containers.internal:8096", True),  # Podman's
    ("", True),                    # no Host: not a browser, not the threat model
    ("evil.com", False),
    ("evil.com:8096", False),
])
def test_host_shapes(host, want) -> None:
    assert auth.host_is_trusted(_Req(host))[0] is want


def test_a_bracketed_ipv6_host_is_parsed_not_mangled() -> None:
    """Splitting on the last colon breaks bare IPv6; stripping brackets after
    splitting breaks the bracketed form. `[::1]:8096` became `::1]:8096` and a
    legitimate IPv6 loopback was refused."""
    assert auth.host_is_trusted(_Req("[::1]:8096"))[0] is True


def test_a_missing_host_header_is_allowed() -> None:
    """HTTP/1.1 requires Host, so its absence means a hand-rolled client — curl,
    a health probe, a script. The rebinding threat model is browser-only, and
    refusing here would break the install script's own readiness poll."""
    assert auth.host_is_trusted(_Req(""))[0] is True


# ---- the escape hatch ------------------------------------------------------- #
def test_a_declared_trusted_host_is_accepted(monkeypatch) -> None:
    """A reverse proxy or a VPN name is a legitimate deployment."""
    from ava_bridge import settings

    monkeypatch.setattr(settings, "get",
                        lambda k, d=None, **kw: (["ava.example.org"]
                                                 if k == "server.trusted_hosts" else d))
    assert auth.host_is_trusted(_Req("ava.example.org:8096"))[0] is True
    assert auth.host_is_trusted(_Req("still-evil.com"))[0] is False


def test_a_string_is_accepted_where_a_list_is_expected(monkeypatch) -> None:
    """Hand-edited YAML: `trusted_hosts: ava.example.org` is what people write."""
    from ava_bridge import settings

    monkeypatch.setattr(settings, "get",
                        lambda k, d=None, **kw: ("ava.example.org"
                                                 if k == "server.trusted_hosts" else d))
    assert auth.host_is_trusted(_Req("ava.example.org"))[0] is True


def test_a_wildcard_opts_out_entirely(monkeypatch) -> None:
    """Honoured deliberately: an owner who needs it should not have to patch the
    code, and a defence people disable by forking is worse than one with a switch."""
    from ava_bridge import settings

    monkeypatch.setattr(settings, "get",
                        lambda k, d=None, **kw: (["*"] if k == "server.trusted_hosts"
                                                 else d))
    ok, why = auth.host_is_trusted(_Req("anything.at.all"))
    assert ok is True and "*" in why


# ---- the gate ordering ------------------------------------------------------ #
def test_the_check_runs_before_every_other_gate() -> None:
    """A rebound request must not reach the origin split or /internal routing.

    Read from the source rather than exercised, because the ordering is the whole
    property and a functional test would pass with the check anywhere.
    """
    import inspect

    src = inspect.getsource(auth.auth_gate)
    body = src.split(":", 1)[1]
    i_host = body.index("host_is_trusted")
    for later in ("apps_origin", '"/internal"', "is_authed"):
        assert later not in body[:i_host], (
            f"{later} is checked before the Host allowlist. A rebound request "
            "would reach it.")


def test_the_refusal_is_421_not_403() -> None:
    """421 Misdirected Request is what "you asked the wrong server for this name"
    means. 403 would tell a legitimate proxy operator they are unauthorized, which
    sends them looking at credentials instead of at their Host header."""
    import inspect

    src = inspect.getsource(auth.auth_gate)
    seg = src[src.index("host_is_trusted"):]
    assert "421" in seg[:400], "the Host refusal no longer uses 421"
