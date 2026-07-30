#!/usr/bin/env python3
"""Security smoke checks for Ava runtime/deploy guardrails.

Run before/after adding services, ports, MCP tools, or egress policies:

    ./ava_security_check.py

The check is intentionally conservative. New exposed ports or broad policies
should be fixed or explicitly documented before they become part of the system.
"""
from __future__ import annotations

import ipaddress
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 11435 is here because an Ollama on the *next* port was found wildcard-bound and
# answering unauthenticated over the tailnet while this check was silent about it:
# the set was a list of ports someone remembered, and the one that mattered was
# the one nobody added. If you run a second inference server, add its port.
SENSITIVE_PORTS = {8001, 8002, 8010, 8096, 8097, 8189, 8888, 9200, 11434, 11435}
SANDBOX_GATEWAY_PORTS = {8010, 8096, 8097, 8189}

# Ports whose whole purpose is to be publicly reachable. Everything else that is
# wildcard-bound gets a notice, because an allowlist-of-nine model made "bound to
# every network this box joins" INVISIBLE for every port nobody thought to add.
# Measured on the development box: seven wildcard listeners, of which exactly one
# (11435) was in SENSITIVE_PORTS. The other six were unreportable by construction.
#
# They are notices, not errors: Ava does not own port 8123 and cannot know whether
# a wildcard bind there is deliberate. But a security check that walks the entire
# socket table and then says nothing about six wildcard binds is worse than one
# that admits it is only looking at nine ports.
CONVENTIONALLY_PUBLIC = {22, 80, 443}

# Tailscale/CGNAT. `ipaddress.is_private` reports **False** for 100.64.0.0/10, so
# before this constant existed the check had no way to express a tailnet bind at
# all — every one landed in the same bucket as `0.0.0.0`, while SECURITY.md §2
# says "any wider exposure (a VPN like Tailscale, or a reverse proxy) is the
# operator's explicit choice". The tool could not represent the posture the
# document blesses, so it reported two deliberate binds as defects and its own
# exit code became noise.
CGNAT = ipaddress.ip_network("100.64.0.0/10")

# RFC1918, spelled out rather than using `ipaddress.is_private`. That property is
# a loose proxy: it also returns True for the RFC 5737 documentation ranges
# (203.0.113.0/24, 198.51.100.0/24, 192.0.2.0/24), so `is_private` would classify
# a bind to 203.0.113.7 as a private sandbox gateway. Harmless in practice, but
# the docstring below says RFC1918 and the code should say what the docstring says.
RFC1918 = tuple(ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
SECRET_FILES = [
    ROOT / ".env",
    ROOT / "data" / "auth_password",
    ROOT / "data" / ".secret",
    ROOT / "data" / ".internal_token",
]
POLICY_DIR = ROOT / "agent" / "policies"


def _host_port(local: str) -> tuple[str, int] | None:
    if local.startswith("[") and "]:" in local:
        host, port = local.rsplit(":", 1)
        return host.strip("[]"), int(port)
    if ":" not in local:
        return None
    host, port = local.rsplit(":", 1)
    try:
        return host, int(port)
    except ValueError:
        return None


def bind_class(host: str, port: int) -> str:
    """Classify a bind address. Four classes, not a boolean.

    `loopback`  — reachable only from this box.
    `gateway`   — an RFC1918 address on a sandbox-gateway port; how the sandbox
                  reaches a host service (on this box the docker gateway,
                  172.27.0.1). Fine.
    `tailnet`   — a Tailscale CGNAT address. Reachable from the operator's other
                  devices and nothing else. SECURITY.md calls this the operator's
                  explicit choice, so it is acceptable *when declared* — see
                  `declared_exposures()`.
    `wildcard`  — 0.0.0.0 / :: / * , i.e. every network this box ever joins.
                  Never acceptable for a sensitive port.
    `other`     — a routable or unparseable address. Treated as worse than
                  tailnet, because nothing here can bound who reaches it.

    A boolean could not distinguish "exposed to my phone" from "exposed to every
    coffee-shop LAN", and both were being reported with the same sentence.
    """
    if host in {"127.0.0.1", "::1", "localhost"}:
        return "loopback"
    if host in {"0.0.0.0", "::", "*"}:
        return "wildcard"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "other"
    if ip.version == 4 and ip in CGNAT:
        return "tailnet"
    if port in SANDBOX_GATEWAY_PORTS and any(ip in n for n in RFC1918):
        return "gateway"
    return "other"


def declared_exposures() -> dict[int, str]:
    """Ports the operator has declared as deliberately exposed, with the reason.

    Read from `$AVA_HOME/ava.yaml` (untracked, box-local):

        security:
          declared_exposures:
            8189: "the GPU service canvas on the tailnet for phone/laptop. NO AUTH."

    **Tracked mechanism, untracked declaration** — deliberately. The rule belongs
    in the repo so every fork enforces it; *which* ports this particular box
    exposes is a fact about one machine and does not belong in a public tree.

    Declaring is not a rubber stamp. It moves the finding from `errors` to a
    printed notice, so the exposure stays visible on every run and the reason has
    to be written down somewhere the checking tool actually reads. The alternative
    on this box was a systemd drop-in comment that no tool has ever read and that
    would not survive a reinstall.
    """
    try:
        from ava_bridge import settings
        raw = settings.get("security.declared_exposures", {}) or {}
    except Exception:  # noqa: BLE001 — no bridge, or an unreadable config
        return {}
    out: dict[int, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
    return out


def parse_listeners(ss_output: str) -> list[tuple[str, int]]:
    """Parse `ss -tlnH` into (host, port). Split out so it can be tested.

    The live socket table is a fact about one machine, so the *logic* was
    untestable while it was welded to the subprocess call — which is why this
    function had no tests at all and the CGNAT gap survived.
    """
    out: list[tuple[str, int]] = []
    for line in ss_output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        hp = _host_port(parts[3])
        if hp:
            out.append(hp)
    return out


def check_ports(errors: list[str], listeners: list[tuple[str, int]] | None = None,
                notices: list[str] | None = None,
                advisories: list[str] | None = None) -> None:
    """Flag sensitive ports bound wider than their class allows.

    Three output channels, because they are three different claims and giving them
    one header is how a security tool starts lying:

    * `errors`     — an Ava port is exposed wider than its class allows. Fails.
    * `notices`    — an Ava port is exposed and the operator DECLARED it. Printed
                     on every run so the decision stays visible. Does not fail.
    * `advisories` — a wildcard bind on a port Ava does not own. Not a finding
                     about Ava at all; reported so it is not invisible.

    `listeners` is injectable, because the live socket table is a fact about one
    machine and the rule must be testable off that machine.
    """
    if listeners is None:
        try:
            out = subprocess.run(["ss", "-tlnH"], capture_output=True, text=True,
                                 timeout=5)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"could not run ss: {exc}")
            return
        listeners = parse_listeners(out.stdout)

    declared = declared_exposures()
    unowned: dict[int, set[str]] = {}
    for host, port in listeners:
        if port not in SENSITIVE_PORTS:
            # Not Ava's port — but a wildcard bind is still worth naming. See
            # CONVENTIONALLY_PUBLIC for why this is a notice and not an error.
            if (bind_class(host, port) == "wildcard"
                    and port not in CONVENTIONALLY_PUBLIC
                    and port not in declared):
                unowned.setdefault(port, set()).add(host)
            continue
        cls = bind_class(host, port)
        if cls in ("loopback", "gateway"):
            continue
        if cls == "wildcard":
            errors.append(
                f"sensitive port {port} is bound to {host} — every network this "
                "box joins. Bind it to 127.0.0.1, or to the specific addresses "
                "you mean. A wildcard bind cannot be declared away.")
            continue
        # tailnet / other
        if port in declared:
            if notices is not None:
                notices.append(
                    f"port {port} is exposed on {host} ({cls}), declared: "
                    f"{declared[port]}")
            continue
        where = ("your tailnet" if cls == "tailnet"
                 else f"{host}, which is neither loopback nor a private gateway")
        errors.append(
            f"sensitive port {port} is bound to {host} — reachable from {where}. "
            "If that is deliberate, declare it in ava.yaml under "
            f"`security.declared_exposures: {{{port}: \"<why, and whether it is "
            "authenticated>\"}` so it stays visible; otherwise bind it to "
            "127.0.0.1.")

    if unowned and advisories is not None:
        for port in sorted(unowned):
            hosts = ", ".join(sorted(unowned[port]))
            advisories.append(
                f"port {port} is wildcard-bound ({hosts}) — reachable from every "
                "network this box joins. Ava does not own this port, so this is "
                "not a finding about Ava.")


def check_secret_permissions(errors: list[str]) -> None:
    for path in SECRET_FILES:
        if not path.exists():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            errors.append(f"{path.relative_to(ROOT)} permissions are {mode:o}; expected 600/owner-only")


def check_policy_wildcards(errors: list[str]) -> None:
    """Flag whole-surface wildcards on Ava's OWN hosts.

    Two changes from the substring scan this replaces, both of which changed the
    result on this box:

    * It goes through `policy_inventory`, so `agent/policies/generated/` and the
      private overlay are checked. They were not — and `generated/` is written by
      code from connector manifests, so it is the directory least likely to have
      been read by a human and the one where an over-broad rule was invisible.
    * It judges the wildcard against the **host** it applies to. `path: "/**"` on
      `api.open-meteo.com` (GET-only, no key) is least privilege: you cannot
      enumerate a third-party API's routes, and the host plus method *is* the
      boundary. The same rule against `host.openshell.internal` hands the sandbox
      the whole internal route table. The old scan flagged the first and, because
      it matched exact quoted substrings, missed a `/**` written any other way.

    A subtree wildcard on an internal host (`/internal/learning/**`) is scoped and
    stays allowed; only the whole surface is an error.
    """
    try:
        from ava_bridge import policy_inventory
    except ImportError as e:  # pragma: no cover - a broken checkout, not a policy
        errors.append(f"cannot import policy_inventory ({e}); wildcards UNCHECKED")
        return
    for pol in policy_inventory.inventory():
        if pol.parse_error:
            errors.append(
                f"{pol.rel} does not parse ({pol.parse_error}); "
                "its rules cannot be checked")
        for w in pol.wildcards:
            if not w.internal:
                continue
            # `/**` or `/internal/**` — the whole surface, or the whole of it.
            if w.path.rstrip("/") in ("/**", "/internal/**"):
                errors.append(
                    f"{pol.rel} allows {w.method} {w.path} on {w.host}; "
                    "enumerate specific route paths instead")


def main() -> int:
    errors: list[str] = []
    notices: list[str] = []
    advisories: list[str] = []
    check_secret_permissions(errors)
    check_policy_wildcards(errors)
    check_ports(errors, notices=notices, advisories=advisories)
    # Declared exposures print on EVERY run, pass or fail. A declaration is a
    # record of a decision, not a way to make it disappear — the point is that the
    # operator keeps seeing what they opened.
    if notices:
        print("Declared exposures (deliberate, still exposed):")
        for n in notices:
            print(f"! {n}")
        print()
    if advisories:
        print("Wildcard binds on ports Ava does not own (not Ava findings):")
        for a in advisories:
            print(f"~ {a}")
        print()
    if errors:
        print("Ava security check FAILED:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Ava security check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())