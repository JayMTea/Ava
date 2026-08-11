"""One enumerator for every egress policy on the box — declared, generated, overlay.

Three callers used to glob for policies independently and they did not agree, in a
way that mattered:

  * `policy_mgmt.list_policies()` globbed `agent/policies/*.yaml` and so the four
    connector policies under `generated/` were invisible to the management API.
  * `ava_security_check.check_policy_wildcards()` globbed the same single level, so
    **an over-broad rule in a generated policy was never checked at all** — and
    `generated/` is the one directory whose contents are written by code from
    connector manifests rather than reviewed by hand.
  * `arch.actual_policies()` walked both levels correctly and was the only one that
    knew the load-bearing detail below, in a comment nobody else read.

**A generated policy's name is `preset.name`, not the filename stem.**
`generated/persona.yaml` declares `preset.name: ava-persona`. Anything keyed on the
stem silently refers to a policy that does not exist by that name in the sandbox.

This module returns *facts* — counts, digests, the wildcard paths it found and the
host each applies to. It deliberately does **not** decide which of them are
acceptable: `ava_security_check` owns that judgement, because "is `/**` too broad"
has different answers for a loopback bridge and for a third-party public API.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, asdict

import yaml

from . import config, settings

ROOT = str(config.ROOT)
POLICY_DIR = os.path.join(ROOT, "agent", "policies")
# Rendered from connector manifests, so it lives under AVA_HOME with the rest of
# the generated agent material rather than in the code root — see
# settings.agent_state_dir(). On a plain checkout the two roots are the same
# directory and this is byte-for-byte the old path.
GENERATED_DIR = settings.generated_policy_dir()
# The private-overlay policies (gitignored first-party apps). Absent on a fork.
OVERLAY_DIR = os.path.join(ROOT, "overlay", "agent", "policies")


def _rel(path: str) -> str:
    """A readable path for reports and the UI.

    Declared and overlay policies hang off the code root; generated ones hang off
    AVA_HOME. Naming the nearest of the two keeps every row reading like
    `agent/policies/generated/acme.yaml` on every install shape, instead of a
    `../../..` climb once the roots differ.
    """
    for base in (ROOT, str(settings.AVA_HOME)):
        try:
            rel = os.path.relpath(path, base)
        except ValueError:      # different drives on Windows
            continue
        if not rel.startswith(os.pardir):
            return rel
    return path

# Hosts that are Ava's own surface rather than somebody else's API. A wildcard
# path against one of these grants the sandbox the whole internal route table;
# against `api.open-meteo.com` it grants GET on a public weather API, where the
# host and method are the actual boundary.
INTERNAL_HOSTS = ("host.openshell.internal", "127.0.0.1", "localhost", "::1",
                  "host.docker.internal")


@dataclass
class Wildcard:
    """A rule whose path contains `**`, with the context needed to judge it."""
    host: str
    port: int | None
    method: str
    path: str
    internal: bool


@dataclass
class Policy:
    name: str                 # preset.name — what the sandbox knows it as
    file_stem: str            # the filename, which for generated/ differs
    source: str               # declared | generated | overlay
    path: str                 # absolute
    rel: str                  # repo-relative, for messages
    sha256: str
    endpoints: int = 0
    rules: int = 0
    binaries: int = 0
    # A policy may declare `network_policies: {}` and gate a tool list instead;
    # without this count it renders as empty, which is how a real grant gets
    # mistaken for dead weight and deleted.
    #
    # The policy that motivated this field, `ava-email-read-only`, turned out NOT
    # to be such a grant: it granted no egress, and its three tools called
    # `/internal/emails`, a route that never existed in the bridge. It was a stub
    # for an unbuilt feature that had never applied to the sandbox, and the
    # provisioning drift report is what finally surfaced that. Removed. The field
    # stays because the shape it guards against is still legitimate.
    tools: int = 0
    wildcards: list[Wildcard] = field(default_factory=list)
    parse_error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_internal(host: str) -> bool:
    return host.lower() in INTERNAL_HOSTS


def _wildcards_from_text(text: str, host: str = "?") -> list[Wildcard]:
    """Fallback for a file that will not parse.

    A policy with a YAML error must not sail past the wildcard check just because
    the parser gave up — that would turn a broken file into a way to hide a rule.
    So an unparsable file is scanned as text, the way the check used to work, and
    the host is reported as unknown (which `_is_internal` treats as external, so
    the checker will still see the path and can decide).
    """
    found: list[Wildcard] = []
    for line in text.splitlines():
        if "**" not in line or "path:" not in line:
            continue
        frag = line.split("path:", 1)[1].strip().strip("}").strip()
        frag = frag.strip('"').strip("'").rstrip(",").strip()
        if "**" in frag:
            found.append(Wildcard(host=host, port=None, method="?", path=frag,
                                  internal=_is_internal(host)))
    return found


def _scan(pol: dict) -> tuple[int, int, int, list[Wildcard]]:
    """Count endpoints/rules/binaries and collect wildcard paths with their host."""
    n_ep = n_rule = n_bin = 0
    wilds: list[Wildcard] = []
    nets = pol.get("network_policies") or {}
    if not isinstance(nets, dict):
        return 0, 0, 0, []
    for entry in nets.values():
        if not isinstance(entry, dict):
            continue
        n_bin += len(entry.get("binaries") or [])
        for ep in entry.get("endpoints") or []:
            if not isinstance(ep, dict):
                continue
            n_ep += 1
            host = str(ep.get("host") or "?")
            port = ep.get("port")
            for rule in ep.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                n_rule += 1
                # `- allow: {method, path}` is the shipped shape; tolerate `deny`
                # and a bare mapping so a format change surfaces as a count, not
                # as a silently skipped rule.
                for body in rule.values():
                    if not isinstance(body, dict):
                        continue
                    path = str(body.get("path") or "")
                    if "**" in path:
                        wilds.append(Wildcard(
                            host=host,
                            port=int(port) if isinstance(port, int) else None,
                            method=str(body.get("method") or "?"),
                            path=path, internal=_is_internal(host)))
    return n_ep, n_rule, n_bin, wilds


def _load_one(path: str, source: str) -> Policy:
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        return Policy(name=stem, file_stem=stem, source=source, path=path,
                      rel=_rel(path), sha256="",
                      parse_error=f"unreadable: {e}")
    pol_obj = Policy(name=stem, file_stem=stem, source=source, path=path,
                     rel=_rel(path), sha256=_digest(raw))
    try:
        pol = yaml.safe_load(raw.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        pol_obj.parse_error = str(e).splitlines()[0][:200]
        pol_obj.wildcards = _wildcards_from_text(
            raw.decode("utf-8", "replace"))
        return pol_obj
    if not isinstance(pol, dict):
        pol_obj.parse_error = "top level is not a mapping"
        return pol_obj
    # The name the sandbox knows it as. For generated/ this is NOT the stem.
    pol_obj.name = ((pol.get("preset") or {}).get("name") or stem)
    (pol_obj.endpoints, pol_obj.rules,
     pol_obj.binaries, pol_obj.wildcards) = _scan(pol)
    tools = pol.get("tools")
    pol_obj.tools = len(tools) if isinstance(tools, list) else 0
    return pol_obj


def inventory() -> list[Policy]:
    """Every policy file on the box, sorted by (source, name). Never raises."""
    out: list[Policy] = []
    for d, source in ((POLICY_DIR, "declared"),
                      (GENERATED_DIR, "generated"),
                      (OVERLAY_DIR, "overlay")):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith((".yaml", ".yml")):
                out.append(_load_one(os.path.join(d, fn), source))
    return sorted(out, key=lambda p: (p.source, p.name))


def names() -> set[str]:
    """The set of policy names the sandbox would know — `preset.name`, not stems."""
    return {p.name for p in inventory()}


def snapshot() -> dict:
    """The `/api/hub/policies/inventory` payload. Facts only, no verdicts."""
    inv = inventory()
    return {
        "policies": [p.to_dict() for p in inv],
        "count": len(inv),
        "by_source": {s: sum(1 for p in inv if p.source == s)
                      for s in ("declared", "generated", "overlay")},
        "wildcards": sum(len(p.wildcards) for p in inv),
        "internal_wildcards": sum(
            1 for p in inv for w in p.wildcards if w.internal),
        "parse_errors": [{"rel": p.rel, "error": p.parse_error}
                         for p in inv if p.parse_error],
        "generated_dir": _rel(GENERATED_DIR),
    }
