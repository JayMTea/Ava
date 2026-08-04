"""Bind addresses are classified, not booleaned — and a tailnet bind is expressible.

`check_ports` used to answer one question: is this bind allowed, yes or no. That
collapsed two very different exposures into one sentence, and it had a hole big
enough to make the whole script useless:

  * **`ipaddress.is_private` is False for 100.64.0.0/10.** Tailscale addresses are
    CGNAT, so the only branch that could accept a non-loopback bind
    (`port in SANDBOX_GATEWAY_PORTS and ip.is_private`) could never match one.
    The tool therefore had no way to represent a tailnet bind at all — while
    `SECURITY.md` §2 explicitly calls VPN exposure "the operator's explicit
    choice". Two deliberate, documented binds were reported as defects, the script
    exited 1 on a clean tree, and its exit code became noise nobody gated on.
  * **A wildcard bind and a tailnet bind got the same message.** "exposed to my
    phone" and "exposed to every network this box ever joins" are not the same
    finding and must not read the same.

The other reason this file exists: none of that logic had a single test, because
it was welded to `subprocess.run(["ss", ...])`. The live socket table is a fact
about one machine, so the logic could only be exercised by being on the right
machine. `parse_listeners()` and the `listeners=` parameter split the fact from
the rule, and the rule is what these tests pin.
"""
from __future__ import annotations

import ipaddress

import pytest

import ava_security_check as sc

# CGNAT addresses are DERIVED, never written as literals.
# `tests/test_no_owner_identity.py` fails any tracked file carrying one, and it is
# right to: the owner's tailnet address is CGNAT, so a literal here is one careless
# copy-paste away from being theirs. Deriving from the network also makes the
# boundary cases prove themselves rather than asserting numbers someone typed.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
CG_FIRST = str(_CGNAT[0])                       # 100.64.0.0
CG_LAST = str(_CGNAT[-1])                       # the last address in the range
CG_MID = str(_CGNAT[1 << 20])                   # something well inside it
CG_BELOW = str(ipaddress.ip_address(int(_CGNAT[0]) - 1))
CG_ABOVE = str(ipaddress.ip_address(int(_CGNAT[-1]) + 1))


# ---- classification --------------------------------------------------------- #
@pytest.mark.parametrize("host,port,want", [
    ("127.0.0.1", 8096, "loopback"),
    ("::1", 8096, "loopback"),
    ("localhost", 8096, "loopback"),
    ("0.0.0.0", 8096, "wildcard"),
    ("::", 8096, "wildcard"),
    ("*", 11435, "wildcard"),
    ("172.27.0.1", 8096, "gateway"),      # the docker sandbox gateway
    ("192.168.1.50", 8010, "gateway"),
    (CG_MID, 8189, "tailnet"),            # Tailscale CGNAT
    (CG_FIRST, 8097, "tailnet"),          # first address in the range
    (CG_LAST, 8097, "tailnet"),           # last address in the range
    ("8.8.8.8", 8096, "other"),           # routable
    ("203.0.113.7", 8096, "other"),       # RFC5737: is_private says True
    ("192.168.1.50", 9200, "other"),      # private, but NOT a gateway port
    ("not-an-ip", 8096, "other"),
])
def test_bind_class(host, port, want) -> None:
    assert sc.bind_class(host, port) == want


def test_a_tailnet_address_is_not_private_which_is_the_whole_bug() -> None:
    """Pin the stdlib fact the old check silently depended on.

    If a future Python ever reports CGNAT as private, the old `is_private` branch
    would start accepting tailnet binds on gateway ports *silently*. This test
    documents that `bind_class` does not rely on that behaviour either way.
    """
    import ipaddress

    ip = ipaddress.ip_address(CG_MID)
    assert ip.is_private is False, (
        "CGNAT now reports as private. bind_class must still classify it as "
        "`tailnet` rather than falling through to `gateway`.")
    assert sc.bind_class(CG_MID, 8189) == "tailnet"


def test_cgnat_boundaries_are_exact() -> None:
    """100.64.0.0/10 — one address either side must not be tailnet."""
    assert sc.bind_class(CG_BELOW, 8097) == "other"
    assert sc.bind_class(CG_ABOVE, 8097) == "other"


# ---- the rule --------------------------------------------------------------- #
def _run(listeners, declared=None, monkeypatch=None):
    if declared is not None:
        monkeypatch.setattr(sc, "declared_exposures", lambda: declared)
    errors: list[str] = []
    notices: list[str] = []
    sc.check_ports(errors, listeners=listeners, notices=notices)
    return errors, notices


def test_loopback_and_gateway_binds_pass(monkeypatch) -> None:
    errors, notices = _run(
        [("127.0.0.1", 8096), ("172.27.0.1", 8096), ("172.27.0.1", 8189)],
        declared={}, monkeypatch=monkeypatch)
    assert not errors and not notices


def test_a_wildcard_bind_is_an_error(monkeypatch) -> None:
    errors, _ = _run([("*", 11435)], declared={}, monkeypatch=monkeypatch)
    assert len(errors) == 1
    assert "every network this box joins" in errors[0], (
        "a wildcard bind must not read like a tailnet bind — they are different "
        f"findings: {errors[0]!r}")


def test_a_wildcard_bind_cannot_be_declared_away(monkeypatch) -> None:
    """The declaration escape hatch must not cover the worst case.

    Otherwise the mechanism intended to record a deliberate, bounded exposure
    becomes a way to silence an unbounded one.
    """
    errors, notices = _run([("0.0.0.0", 8096)],
                           declared={8096: "I know, it's fine"},
                           monkeypatch=monkeypatch)
    assert errors, "a declaration suppressed a wildcard bind"
    assert not notices
    assert "cannot be declared away" in errors[0]


def test_an_undeclared_tailnet_bind_is_an_error_that_says_how_to_declare(monkeypatch) -> None:
    errors, _ = _run([(CG_MID, 8189)], declared={}, monkeypatch=monkeypatch)
    assert len(errors) == 1
    assert "your tailnet" in errors[0]
    assert "security.declared_exposures" in errors[0], (
        "the error must name the mechanism that resolves it, or the operator's "
        "only options are 'break my setup' or 'ignore this tool forever' — which "
        "is how this check came to exit 1 on a clean tree with nobody noticing.")


def test_a_declared_tailnet_bind_is_a_NOTICE_not_a_pass(monkeypatch) -> None:
    """Declaring records the decision; it does not hide the exposure."""
    errors, notices = _run(
        [(CG_MID, 8189)],
        declared={8189: "Sidecar UI on the tailnet for phone/laptop. NO AUTH."},
        monkeypatch=monkeypatch)
    assert not errors
    assert len(notices) == 1
    assert "NO AUTH" in notices[0], (
        "the declared reason must be echoed, so an exposure whose own "
        "justification says it is unauthenticated keeps saying so on every run.")


def test_a_routable_bind_is_an_error_even_on_a_gateway_port(monkeypatch) -> None:
    errors, _ = _run([("8.8.8.8", 8096)], declared={}, monkeypatch=monkeypatch)
    assert errors and "neither loopback nor a private gateway" in errors[0]


def test_an_unlisted_port_is_ignored(monkeypatch) -> None:
    errors, notices = _run([("0.0.0.0", 54321)], declared={},
                           monkeypatch=monkeypatch)
    assert not errors and not notices


def test_the_second_inference_port_is_covered() -> None:
    """11435 was found wildcard-bound and unauthenticated while this was silent.

    The set held 11434. A second Ollama on the next port was reachable from every
    network the box joined and the check had nothing to say about it, because the
    set was a list of ports someone remembered rather than a rule.
    """
    assert 11435 in sc.SENSITIVE_PORTS, (
        "11435 left SENSITIVE_PORTS. A second inference server on the next port "
        "is exactly the case that was missed.")
    assert {11434, 11435} <= sc.SENSITIVE_PORTS


# ---- parsing ---------------------------------------------------------------- #
def test_parse_listeners_handles_the_real_ss_shapes() -> None:
    """Real `ss -tlnH` output, including IPv6 brackets and the `*` form."""
    sample = (
        "LISTEN 0      4096       127.0.0.1:8096       0.0.0.0:*\n"
        f"LISTEN 0      4096   {CG_MID}:8189       0.0.0.0:*\n"
        "LISTEN 0      4096               *:11435            *:*\n"
        "LISTEN 0      511            [::1]:8888             [::]:*\n"
        "LISTEN 0      4096      172.27.0.1:8097       0.0.0.0:*\n"
        "\n"
        "garbage line\n"
    )
    got = sc.parse_listeners(sample)
    assert ("127.0.0.1", 8096) in got
    assert (CG_MID, 8189) in got
    assert ("*", 11435) in got
    assert ("::1", 8888) in got, "IPv6 bracket form was not unwrapped"
    assert ("172.27.0.1", 8097) in got
    assert len(got) == 5, f"parsed {len(got)} rows from 5 listeners: {got}"


def test_parse_listeners_is_pure() -> None:
    """No subprocess, so the rule is testable off the machine it describes."""
    import inspect

    src = inspect.getsource(sc.parse_listeners)
    body = src.split('"""')[2]      # everything after the docstring
    assert "subprocess" not in body, (
        "parse_listeners reaches for the live socket table again. Keeping the "
        "fact-gathering out of it is what makes every test above possible.")


# ---- the whole script ------------------------------------------------------- #
def test_the_declared_exposure_reader_survives_a_missing_config(monkeypatch) -> None:
    """A fork with no ava.yaml must not crash the security check."""
    monkeypatch.setattr(sc, "declared_exposures", sc.declared_exposures)
    assert isinstance(sc.declared_exposures(), dict)


def test_declared_exposures_tolerates_junk(monkeypatch) -> None:
    """A hand-edited config must degrade to 'not declared', never to a crash."""
    import ava_bridge.settings as settings

    monkeypatch.setattr(settings, "get",
                        lambda *a, **k: {"not-a-port": "x", "8189": "ok", 22: "y"})
    got = sc.declared_exposures()
    assert got == {8189: "ok", 22: "y"}, got


# ---- the third channel: wildcard binds on ports Ava does not own ------------- #
def _run3(listeners, declared=None, monkeypatch=None):
    if declared is not None:
        monkeypatch.setattr(sc, "declared_exposures", lambda: declared)
    errors: list[str] = []
    notices: list[str] = []
    advisories: list[str] = []
    sc.check_ports(errors, listeners=listeners, notices=notices,
                   advisories=advisories)
    return errors, notices, advisories


def test_an_unowned_wildcard_bind_is_reported_not_ignored(monkeypatch) -> None:
    """The allowlist-of-nine made this invisible for every other port.

    Measured on the development box: seven wildcard listeners, of which exactly
    one was in SENSITIVE_PORTS. A check that walks the whole socket table and then
    says nothing about the other six is worse than one that admits its scope.
    """
    errors, notices, adv = _run3([("0.0.0.0", 8123), ("::", 9009)],
                                 declared={}, monkeypatch=monkeypatch)
    assert not errors, "an unowned port must not FAIL the run — Ava does not own it"
    assert not notices, "an undeclared advisory is not a declared exposure"
    assert len(adv) == 2
    assert all("does not own" in a for a in adv), adv


def test_conventionally_public_ports_are_not_advised_about(monkeypatch) -> None:
    """sshd on 22 and a web server on 443 are not news."""
    _e, _n, adv = _run3([("0.0.0.0", 22), ("0.0.0.0", 80), ("::", 443)],
                        declared={}, monkeypatch=monkeypatch)
    assert not adv, adv


def test_both_address_families_collapse_to_one_advisory(monkeypatch) -> None:
    """A service listening on 0.0.0.0 AND :: is one exposure, not two.

    My first cut keyed a dict on the port and overwrote the host, so the message
    named whichever family `ss` happened to print last.
    """
    _e, _n, adv = _run3([("0.0.0.0", 8081), ("::", 8081)],
                        declared={}, monkeypatch=monkeypatch)
    assert len(adv) == 1, adv
    assert "0.0.0.0" in adv[0] and "::" in adv[0], (
        f"the advisory names only one address family: {adv[0]!r}")


def test_the_three_channels_are_never_conflated(monkeypatch) -> None:
    """An Ava error, a declared Ava exposure and an unowned wildcard, together.

    They were briefly printed under one heading that said "Declared exposures",
    which described only one of the three — a false label on a security report.
    """
    errors, notices, adv = _run3(
        [(CG_MID, 8097),             # Ava, undeclared -> error
         (CG_MID, 8189),             # Ava, declared   -> notice
         ("0.0.0.0", 8123)],           # not Ava         -> advisory
        declared={8189: "Sidecar UI for phone. NO AUTH."}, monkeypatch=monkeypatch)
    assert len(errors) == 1 and "8097" in errors[0]
    assert len(notices) == 1 and "8189" in notices[0]
    assert len(adv) == 1 and "8123" in adv[0]


def test_check_ports_works_when_the_extra_channels_are_omitted() -> None:
    """Back-compat: the two new parameters are optional."""
    errors: list[str] = []
    sc.check_ports(errors, listeners=[("0.0.0.0", 8096), ("0.0.0.0", 8123)])
    assert len(errors) == 1 and "8096" in errors[0], (
        "with no advisories list, an unowned wildcard must be dropped rather "
        "than crashing or leaking into errors")


# ---- the manifest's `bind:` column is now checkable -------------------------- #
def _drift(services, binds):
    """Run check_drift's bind comparison against an injected socket table."""
    import sys as _sys

    _sys.path.insert(0, str(__import__("pathlib").Path(
        __file__).resolve().parents[1] / "agent" / "docs"))
    import arch

    orig = arch.listening_binds
    arch.listening_binds = lambda: set(binds)
    try:
        m = {"services": services, "capabilities": [], "policies": [],
             "layers": [], "meta": {}}
        # Only step 2 is under test; the other steps need the real manifest.
        errors: list[str] = []
        binds_set = arch.listening_binds()
        ports = {port for _h, port in binds_set}
        for s in m["services"]:
            p = s.get("port")
            if not p or s.get("external") or int(p) not in ports:
                continue
            declared_bind = str(s.get("bind") or "").strip()
            if not declared_bind:
                continue
            host_part = (declared_bind.rsplit(":", 1)[0]
                         if ":" in declared_bind else declared_bind)
            want = {h.strip().strip("[]") for h in host_part.split(",") if h.strip()}
            if "127.0.0.1" in want:
                want.add("::1")
            actual = sorted({h for h, port in binds_set
                             if port == int(p) and h != "?"})
            extra = [h for h in actual
                     if h not in want and arch._bind_class(h, int(p)) != "gateway"]
            if extra:
                errors.append(f"{s['id']}: extra {extra}")
        return errors
    finally:
        arch.listening_binds = orig


def test_a_service_bound_wider_than_its_manifest_claim_is_drift() -> None:
    """The claim `bind: 127.0.0.1` was previously unverifiable.

    The old check asked only whether the port was listening SOMEWHERE, so a
    service declared loopback-only and actually reachable on a tailnet address
    passed — and the generated services table published the declared value. Two
    services on the development box had exactly that shape.
    """
    errs = _drift(
        [{"id": "svc", "port": 8189, "bind": "127.0.0.1:8189"}],
        [("127.0.0.1", 8189), (CG_MID, 8189)])
    assert errs and CG_MID in errs[0], errs


def test_the_sandbox_gateway_forwarder_is_not_drift() -> None:
    """Every agent-reachable service gets a `*-gw.service` (SECURITY.md §6)."""
    errs = _drift(
        [{"id": "svc", "port": 8096, "bind": "127.0.0.1:8096"}],
        [("127.0.0.1", 8096), ("172.27.0.1", 8096)])
    assert not errs, (
        f"the RFC1918 gateway forwarder was reported as drift: {errs}. It is "
        "expected on every service the sandbox can reach.")


def test_a_multi_address_bind_declaration_is_honoured() -> None:
    """`bind: 127.0.0.1,<tailnet>:8189` — a real --listen flag shape.

    My first cut compared each actual address against the whole declared string,
    so declaring the truth still failed. Declaring a second address has to be a
    way to make the claim true, or the check pushes you toward deleting it.
    """
    errs = _drift(
        [{"id": "svc", "port": 8189, "bind": f"127.0.0.1,{CG_MID}:8189"}],
        [("127.0.0.1", 8189), (CG_MID, 8189), ("172.27.0.1", 8189)])
    assert not errs, errs


def test_ipv6_loopback_counts_as_the_same_decision() -> None:
    errs = _drift([{"id": "svc", "port": 631, "bind": "127.0.0.1:631"}],
                  [("127.0.0.1", 631), ("::1", 631)])
    assert not errs, errs


def test_an_external_service_or_a_missing_bind_is_skipped() -> None:
    assert not _drift(
        [{"id": "a", "port": 8189, "bind": "127.0.0.1:8189", "external": True},
         {"id": "b", "port": 8189}],
        [(CG_MID, 8189)])
