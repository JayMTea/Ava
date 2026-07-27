"""The bridge container must bind 0.0.0.0 inside its own network namespace.

This reads backwards, so the reasoning is worth stating: the container's netns IS
the security boundary. compose publishes `127.0.0.1:8096:8096`, which is what
keeps the port host-local. Binding 127.0.0.1 *inside* the namespace as well means
the published port has nothing listening behind it — and the failure is silent,
because the image's own HEALTHCHECK curls that same in-namespace loopback and
passes. `docker compose up` produced a container reporting `healthy` and a
browser that could not connect, after a multi-GB pull.

It shipped because nothing seeded AVA_HOST: .dockerignore excludes .env and
ava.yaml, /data is a fresh volume, and ava_bridge/config.py defaults to
127.0.0.1. The maintainer's bare-metal box never sees it — ava.yaml there carries
an explicit host — so only a static check catches it.

Also guards two adjacent supply-chain footguns in the same tree: an ARG that
nothing reads (NEMOCLAW_INSTALL_TAG was declared, never referenced, and the
entrypoint therefore piped the upstream `main` branch into bash at every first
start), and a `curl | bash` whose ref is not pinned anywhere.

Style matches tests/test_no_eval_data.py: a `git ls-files` scan, no bridge, no
AVA_HOME, no Docker.
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

_ENV_AVA_HOST = re.compile(r"^\s*(?:ENV\s+)?AVA_HOST=0\.0\.0\.0", re.MULTILINE)
_HEALTHCHECK_LOOPBACK = re.compile(r"HEALTHCHECK[\s\S]{0,300}?127\.0\.0\.1")
_ARG = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
_CURL_BASH = re.compile(r"curl[^\n|]*\|\s*bash")
# The URL curl itself is given, skipping any flags between.
_CURL_TARGET = re.compile(r"curl\s+(?:-\S+\s+)*[\"']?(https://[^\s\"']+)")


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def test_bridge_image_binds_all_interfaces_in_its_namespace() -> None:
    src = _read("deploy/Dockerfile")
    assert src, "deploy/Dockerfile not found — did the image move?"
    assert _ENV_AVA_HOST.search(src), (
        "deploy/Dockerfile must set AVA_HOST=0.0.0.0 in its ENV block. Without "
        "it, ava_bridge/config.py's 127.0.0.1 default applies INSIDE the "
        "container namespace, the published 127.0.0.1:8096:8096 port refuses "
        "every connection, and the HEALTHCHECK still reports healthy because it "
        "curls the same in-namespace loopback. This is not a wider bind: the "
        "compose `ports:` entry is what scopes the port to the host.")


def test_compose_restates_the_bind_next_to_the_port_mapping() -> None:
    src = _read("deploy/docker-compose.yml")
    assert "AVA_HOST:" in src, (
        "deploy/docker-compose.yml's `ava` service must set AVA_HOST explicitly. "
        "Inheriting it from the image works, but the bind and the port mapping "
        "are one contract and belong in one place.")
    assert re.search(r'ports:\s*\n\s*-\s*"127\.0\.0\.1:8096:8096"', src), (
        "the bridge port must be published on 127.0.0.1 only — publishing on "
        "0.0.0.0 with AVA_HOST=0.0.0.0 exposes an unauthenticated first-run "
        "/setup to the whole LAN")


def test_a_loopback_healthcheck_never_stands_alone() -> None:
    """A healthcheck that curls in-namespace loopback proves the process is up,
    not that anyone can reach it. It is only honest alongside a 0.0.0.0 bind.

    Scoped to the bridge image on purpose. deploy/agent.Dockerfile also
    healthchecks its own loopback, but ava_bridge/agent_runtime_server.py:90
    defaults AVA_AGENT_BIND to 0.0.0.0 in code, so that image has no equivalent
    gap — it is the *config default* being loopback that made the bridge fail.
    """
    src = _read("deploy/Dockerfile")
    if _HEALTHCHECK_LOOPBACK.search(src):
        assert _ENV_AVA_HOST.search(src), (
            "deploy/Dockerfile healthchecks its own loopback without binding "
            "0.0.0.0, so the healthcheck can report healthy while every "
            "published-port connection is refused")


def test_every_declared_arg_is_actually_used() -> None:
    """`ARG NEMOCLAW_INSTALL_TAG=latest` sat in agent.Dockerfile referenced by
    nothing, while the entrypoint read a differently-named variable and fell
    back to the upstream `main` branch."""
    offenders: list[str] = []
    for rel in _tracked("deploy/*Dockerfile*"):
        src = _read(rel)
        for arg in _ARG.findall(src):
            # Used if referenced after declaration, or exported via ENV.
            uses = len(re.findall(rf"\$\{{?{arg}\b", src))
            if uses == 0:
                offenders.append(f"{rel}: ARG {arg} is declared but never referenced")
    assert not offenders, (
        "a declared-but-unreferenced ARG is a build knob that silently does "
        "nothing — either wire it through (ENV/interpolation) or delete it: "
        f"{offenders}")


def test_curl_into_bash_uses_a_pinned_ref() -> None:
    """A `curl … | bash` in a shipped script must install a ref somebody chose,
    not whatever a moving branch happens to be at container start."""
    pinned_vars: set[str] = set()
    for rel in _tracked("deploy/*Dockerfile*"):
        for m in re.finditer(r"^\s*(?:ARG|ENV)\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)",
                             _read(rel), re.MULTILINE):
            name, value = m.group(1), m.group(2)
            if value not in {"main", "master", "latest", "HEAD"} and "${" not in value:
                pinned_vars.add(name)

    offenders: list[str] = []
    for rel in _tracked("deploy/*.sh"):
        for line in _read(rel).splitlines():
            # Comments document the invocation (install.sh's own header shows a
            # `curl … | bash` one-liner); they install nothing.
            if line.lstrip().startswith("#") or not _CURL_BASH.search(line):
                continue
            # The URL curl actually fetches, not merely the first URL on the line
            # — an env assignment before the command would otherwise be read as
            # the target.
            target = _CURL_TARGET.search(line)
            if not target:
                continue
            url = target.group(1)
            refs = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", url)
            if not refs:
                offenders.append(f"{rel}: hardcoded ref in {url}")
            elif not any(r in pinned_vars for r in refs):
                offenders.append(
                    f"{rel}: {refs[0]} is not pinned by any deploy/*Dockerfile ARG/ENV")

    assert not offenders, (
        "these pipe a remote script into bash without a ref pinned in an image "
        "build arg, so the same image installs different code on different days "
        f"— {offenders}")
