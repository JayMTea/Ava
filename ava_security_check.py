#!/usr/bin/env python3
"""Security smoke checks for Ava runtime/deploy guardrails.

Run before/after adding services, ports, MCP tools, or egress policies:

    ./ava_security_check.py

The check is intentionally conservative. New exposed ports or broad policies
should be fixed or explicitly documented before they become part of the system.
"""
from __future__ import annotations

import ipaddress
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SENSITIVE_PORTS = {8001, 8002, 8010, 8096, 8097, 8189, 8888, 9200, 11434}
SANDBOX_GATEWAY_PORTS = {8010, 8096, 8097, 8189}
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


def _allowed_bind(host: str, port: int) -> bool:
    if host in {"127.0.0.1", "::1", "localhost"}:
        return True
    if host in {"0.0.0.0", "::", "*"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return port in SANDBOX_GATEWAY_PORTS and ip.is_private


def check_ports(errors: list[str]) -> None:
    try:
        out = subprocess.run(["ss", "-tlnH"], capture_output=True, text=True, timeout=5)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"could not run ss: {exc}")
        return
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        hp = _host_port(parts[3])
        if not hp:
            continue
        host, port = hp
        if port in SENSITIVE_PORTS and not _allowed_bind(host, port):
            errors.append(f"sensitive port {port} is bound to {host}; expected loopback or sandbox gateway")


def check_secret_permissions(errors: list[str]) -> None:
    for path in SECRET_FILES:
        if not path.exists():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            errors.append(f"{path.relative_to(ROOT)} permissions are {mode:o}; expected 600/owner-only")


def check_policy_wildcards(errors: list[str]) -> None:
    if not POLICY_DIR.exists():
        return
    for path in POLICY_DIR.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)
        if 'path: "/internal/**"' in text:
            errors.append(f"{rel} allows /internal/**; enumerate route paths instead")
        if 'path: "/**"' in text:
            errors.append(f"{rel} allows /**; enumerate specific route paths instead")


def check_removed_containers(errors: list[str]) -> None:
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"could not inspect Docker containers: {exc}")
        return
    names = {line.strip() for line in out.stdout.splitlines() if line.strip()}
    # Ava is Omni-only: her single always-on brain is the open-model 30B engine.
    # The retired Super/Nano models must not run ALONGSIDE it — together they
    # exceed the unified memory pool. (They may still run standalone for other
    # apps before the always-on model switch; only the co-resident case is unsafe.)
    retired = names & {"vllm-super", "vllm-nano"}
    if "vllm-open" in names and retired:
        errors.append(
            f"retired {sorted(retired)} running alongside vllm-open — they exceed "
            "the unified memory pool together; stop them (Ava is Omni-only)")


def main() -> int:
    errors: list[str] = []
    check_secret_permissions(errors)
    check_policy_wildcards(errors)
    check_removed_containers(errors)
    check_ports(errors)
    if errors:
        print("Ava security check FAILED:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Ava security check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())