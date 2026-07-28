"""A profile must start the service its own backend URL points at.

The `ava` service has no `profiles:` key, so it runs under every profile. That
made its backend settings global, and the single default they carried —
`AVA_BACKEND_URL=http://vllm:8002/v1` — was correct for exactly one profile. Under
`--profile cpu`, which deploy/install.sh selects for every GPU-less box and every
card under 16 GB, no `vllm` service exists, so the bridge spent every turn
resolving a hostname that would never exist. After a multi-GB pull, with nothing
in the UI saying why.

Nothing caught it because `docker compose config` is perfectly happy to
interpolate a default that names a service the selected profile never starts.
Comparing the two is what this file does.

The backend trio now has no defaults at all (`${VAR:?...}`), and lives in
deploy/profiles/<profile>.env instead. That only works while every profile file
actually sets every guarded variable, which is the other half of this guard.

Style matches tests/test_no_eval_data.py: a `git ls-files` scan, no bridge, no
AVA_HOME, no Docker daemon — PyYAML is already a runtime dependency, so the
compose file is parsed directly.
"""
import pathlib
import re
import subprocess
from urllib.parse import urlparse

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "docker-compose.yml"

# `${NAME:?message}` — a variable compose refuses to interpolate without a value.
_REQUIRED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?")

# Values a profile may legitimately ship empty, because only the user knows them.
_MAY_BE_EMPTY = {"cloud.env": {"AVA_BACKEND_URL", "AVA_MODEL", "AVA_INFERENCE_KEY"}}


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _profile_env(rel: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _services_started_by(profile: str, compose: dict) -> set[str]:
    started = set()
    for name, svc in (compose.get("services") or {}).items():
        declared = svc.get("profiles")
        if not declared or profile in declared:
            started.add(name)
    return started


def test_profile_files_exist_for_every_declared_profile() -> None:
    compose = _compose()
    declared = set()
    for svc in (compose.get("services") or {}).values():
        declared.update(svc.get("profiles") or [])
    declared.add("cloud")          # starts only the always-on services

    shipped = {pathlib.Path(p).stem for p in _tracked("deploy/profiles/*.env")}
    missing = sorted(declared - shipped)
    extra = sorted(shipped - declared)
    assert not missing and not extra, (
        "deploy/profiles/*.env must match the profiles declared in "
        f"deploy/docker-compose.yml — missing {missing}, unexpected {extra}")


def test_every_required_variable_is_supplied_by_every_profile() -> None:
    required = set(_REQUIRED.findall(COMPOSE.read_text(encoding="utf-8")))
    assert required, (
        "no `${VAR:?...}` guards found in deploy/docker-compose.yml — the backend "
        "trio must stay guarded, or a wrong default silently returns")

    offenders: list[str] = []
    for rel in _tracked("deploy/profiles/*.env"):
        env = _profile_env(rel)
        exempt = _MAY_BE_EMPTY.get(pathlib.Path(rel).name, set())
        for var in sorted(required):
            if var not in env:
                offenders.append(f"{rel}: missing {var}")
            elif not env[var] and var not in exempt:
                offenders.append(f"{rel}: {var} is empty")
    assert not offenders, (
        "compose refuses to interpolate these, so a profile that omits one "
        f"cannot start at all — {offenders}")


def test_each_profile_points_at_an_engine_it_actually_starts() -> None:
    """The regression test for the original defect."""
    compose = _compose()
    service_names = set((compose.get("services") or {}).keys())

    offenders: list[str] = []
    for rel in _tracked("deploy/profiles/*.env"):
        env = _profile_env(rel)
        url = env.get("AVA_BACKEND_URL", "")
        if not url:
            continue                     # cloud ships empty on purpose
        host = (urlparse(url).hostname or "")
        if host not in service_names:
            continue                     # an external/cloud endpoint, not ours
        profiles = [p for p in env.get("COMPOSE_PROFILES", "").split(",") if p]
        started: set[str] = set()
        for p in profiles:
            started |= _services_started_by(p, compose)
        if host not in started:
            offenders.append(
                f"{rel}: AVA_BACKEND_URL points at '{host}', which profiles "
                f"{profiles} never start (they start: {sorted(started)})")

    assert not offenders, (
        "these profiles wire the bridge to a compose service the profile does not "
        "bring up, so every chat turn resolves a hostname that does not exist — "
        f"{offenders}")


def test_every_profile_selects_itself() -> None:
    compose = _compose()
    declared = {"cloud"}
    for svc in (compose.get("services") or {}).values():
        declared.update(svc.get("profiles") or [])

    offenders: list[str] = []
    for rel in _tracked("deploy/profiles/*.env"):
        env = _profile_env(rel)
        value = env.get("COMPOSE_PROFILES", "")
        if not value:
            offenders.append(f"{rel}: no COMPOSE_PROFILES (compose would start no engine)")
            continue
        stem = pathlib.Path(rel).stem
        parts = [p for p in value.split(",") if p]
        if stem not in parts:
            offenders.append(f"{rel}: COMPOSE_PROFILES={value!r} does not include '{stem}'")
        for p in parts:
            if p not in declared:
                offenders.append(f"{rel}: COMPOSE_PROFILES names unknown profile '{p}'")
    assert not offenders, (
        "COMPOSE_PROFILES is what makes `docker compose up -d` (with no flags) "
        f"start the right services — {offenders}")


def test_the_bridge_port_is_published_on_loopback_only() -> None:
    ava = (_compose().get("services") or {}).get("ava") or {}
    ports = [str(p) for p in (ava.get("ports") or [])]
    assert ports, "the ava service publishes no port"
    offenders = [p for p in ports if not p.startswith("127.0.0.1:")]
    assert not offenders, (
        "the bridge must publish on 127.0.0.1 only. It binds 0.0.0.0 INSIDE its "
        "namespace (deploy/Dockerfile), so a 0.0.0.0 publish here would expose an "
        f"unauthenticated first-run /setup to the whole network — {offenders}")
