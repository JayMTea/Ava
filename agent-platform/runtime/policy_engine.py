"""Hard enforcement of policies/*.yaml.

Instructions (AGENT.md, instructions.md, SKILL.md) are *soft*: they ask the model to
behave. Everything in this module is *hard*: it runs in code, before a tool executes,
and the model cannot talk its way past it. When the two disagree, this module wins.

Every check fails closed: missing config, unknown tools, unresolvable hosts and
unmatched paths are all denied.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .types import PolicyViolation, ToolSpec


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def glob_match(path: str, pattern: str) -> bool:
    """fnmatch with `**/` also matching zero directories. Case-insensitive, so `.ENV` on a
    case-insensitive filesystem cannot slip past a `.env` deny rule. `*` crosses `/`."""
    path, pattern = path.lower(), pattern.lower()
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
        return True
    return pattern.endswith("/**") and path == pattern[:-3]


def _agent_matches(rule: dict[str, Any], agent: str) -> bool:
    return any(fnmatch.fnmatchcase(agent, a) for a in rule.get("agents", []))


def _tool_matches(rule: dict[str, Any], tool_id: str) -> bool:
    return any(fnmatch.fnmatchcase(tool_id, t) for t in rule.get("tools", []))


class PolicyEngine:
    def __init__(self, policies: dict[str, dict[str, Any]], workspace: Path):
        self.policies = policies
        self.workspace = Path(workspace).resolve()
        self._redactions = [
            (p["name"], re.compile(p["regex"]))
            for p in self.safety.get("redaction", {}).get("patterns", [])
        ]

    @property
    def tool_permissions(self) -> dict[str, Any]:
        return self.policies.get("tool_permissions", {})

    @property
    def approvals(self) -> dict[str, Any]:
        return self.policies.get("approvals", {})

    @property
    def data_access(self) -> dict[str, Any]:
        return self.policies.get("data_access", {})

    @property
    def network(self) -> dict[str, Any]:
        return self.policies.get("network", {})

    @property
    def safety(self) -> dict[str, Any]:
        return self.policies.get("safety", {})

    @property
    def limits(self) -> dict[str, Any]:
        return self.safety.get("limits", {})

    # -- tools -----------------------------------------------------------------------------

    def check_tool(self, agent: str, tool_id: str) -> Decision:
        """Deny rules win over grants; anything not granted is denied."""
        perms = self.tool_permissions
        for rule in perms.get("denies", []):
            if _agent_matches(rule, agent) and _tool_matches(rule, tool_id):
                reason = rule.get("reason", "explicitly denied")
                return Decision(False, f"policies/tool-permissions.yaml denies {tool_id} to {agent}: {reason}")
        for rule in perms.get("grants", []):
            if _agent_matches(rule, agent) and _tool_matches(rule, tool_id):
                return Decision(True, "granted")
        return Decision(False, f"policies/tool-permissions.yaml does not grant {tool_id} to {agent} (default: deny)")

    def requires_approval(self, spec: ToolSpec) -> bool:
        cfg = self.approvals
        if spec.id in cfg.get("auto_approve", {}).get("tools", []):
            return False
        required = cfg.get("require_approval", {})
        if any(fnmatch.fnmatchcase(spec.id, t) for t in required.get("tools", [])):
            return True
        return spec.side_effects in required.get("side_effects", [])

    # -- filesystem ------------------------------------------------------------------------

    def resolve_path(self, path: str, mode: str) -> Path:
        """Resolve a workspace-relative path and enforce data-access.yaml. mode: read|write."""
        if not path or "\x00" in path:
            raise PolicyViolation("empty or invalid path")
        candidate = Path(path)
        target = (candidate if candidate.is_absolute() else self.workspace / candidate).resolve()
        if not target.is_relative_to(self.workspace):
            raise PolicyViolation(f"{path!r} is outside the workspace")
        rel = target.relative_to(self.workspace).as_posix() or "."
        fs = self.data_access.get("filesystem", {})
        if any(glob_match(rel, p) for p in fs.get(f"{mode}_deny", [])):
            raise PolicyViolation(f"{mode} access to {rel!r} is denied by policies/data-access.yaml")
        if not any(glob_match(rel, p) for p in fs.get(f"{mode}_allow", [])):
            raise PolicyViolation(f"{mode} access to {rel!r} is not allowed by policies/data-access.yaml")
        return target

    # -- network ---------------------------------------------------------------------------

    def check_url(self, url: str) -> None:
        """Enforce network.yaml for agent-initiated requests (not model-provider traffic).

        Resolution happens here and again inside the HTTP client, so a hostile DNS server
        could answer differently the second time (rebinding). Pin resolved IPs in a proxy
        if that threat matters for your deployment.
        """
        cfg = self.network
        parts = urlsplit(url)
        if parts.scheme not in cfg.get("allow_schemes", ["https"]):
            raise PolicyViolation(f"scheme {parts.scheme!r} is not allowed by policies/network.yaml")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise PolicyViolation(f"no host in {url!r}")
        if cfg.get("default", "deny") != "allow":
            allowed = cfg.get("allow_domains", [])
            if not any(host == d or host.endswith("." + d) for d in allowed):
                raise PolicyViolation(f"{host} is not in policies/network.yaml allow_domains")
        if cfg.get("deny_private_networks", True):
            for addr in self._resolve(host):
                if not addr.is_global:
                    raise PolicyViolation(f"{host} resolves to non-public address {addr}")

    @staticmethod
    def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise PolicyViolation(f"cannot resolve {host}: {exc}") from exc
        return [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]

    # -- content ---------------------------------------------------------------------------

    def redact(self, text: str) -> str:
        for name, pattern in self._redactions:
            text = pattern.sub(f"[REDACTED:{name}]", text)
        return text

    @property
    def wrap_untrusted(self) -> bool:
        return self.safety.get("untrusted_content", {}).get("wrap_tool_output", True)
