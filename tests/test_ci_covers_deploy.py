"""CI must exercise the install path README.md recommends first.

For most of this repo's life nothing in CI ran `docker compose`, built
deploy/agent.Dockerfile, or ran the qa/ tier. Four of the ten blockers found in
the July 2026 audit lived in deploy/docker-compose.yml, and every one of them was
invisible on the maintainer's bare-metal box. That single gap explains them.

The subtler lesson is in `--network host`. A compose-smoke job did exist for a
while, and it booted the image with `docker run --network host` — which puts the
container in the runner's own network namespace, the one mode where a process
bound to in-namespace loopback IS reachable at 127.0.0.1. So it could not detect
that the image bound loopback inside its namespace and left the published port
refusing every connection. A test that cannot fail is worse than no test: it
reports coverage it does not have. Hence the assertion below.

Style matches tests/test_diagram_sync.py: a `git ls-files` scan, no bridge, no
AVA_HOME, no Docker daemon.
"""
import pathlib
import re
import subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"

# Jobs that exist specifically to cover deploy/ and the real ASGI app.
_REQUIRED_JOBS = {
    "qa":             "the only tier that imports phone_bridge.py and drives the real app",
    "compose-config": "parses every profile and checks each points at an engine it starts",
    "compose-smoke":  "builds both images and boots through the published port",
    "install-smoke":  "proves deploy/install.sh writes a valid .env and preserves secrets",
    "shellcheck":     "deploy/ is several hundred lines of shell that every install runs",
}


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _uncommented(text: str) -> str:
    """Workflow lines with `#` comments stripped, so prose about a banned
    pattern does not read as a use of it."""
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
        out.append(stripped)
    return "\n".join(out)


def test_ci_defines_the_jobs_that_cover_deploy() -> None:
    jobs = set((yaml.safe_load(CI.read_text(encoding="utf-8")).get("jobs") or {}).keys())
    missing = sorted(set(_REQUIRED_JOBS) - jobs)
    assert not missing, (
        "these CI jobs are gone, and with them the only automated coverage of "
        "the documented install path — "
        + "; ".join(f"{m}: {_REQUIRED_JOBS[m]}" for m in missing))


def test_the_compose_boot_does_not_use_host_networking() -> None:
    body = _uncommented(CI.read_text(encoding="utf-8"))
    assert "--network host" not in body, (
        "a compose/boot step uses `--network host`. That shares the runner's "
        "network namespace, so a container bound to in-namespace loopback is "
        "still reachable at 127.0.0.1 — which is exactly the bug this job exists "
        "to catch, and why it went undetected. Boot through `docker compose up` "
        "on the default bridge network and curl the PUBLISHED port instead.")


def test_the_compose_boot_probes_the_published_port() -> None:
    body = _uncommented(CI.read_text(encoding="utf-8"))
    assert "docker compose up -d" in body, (
        "compose-smoke must boot via `docker compose up -d` — a bare `docker run` "
        "never exercises the `ports:` mapping, which is where the bind bug lived")
    assert "127.0.0.1:8096/api/health" in body, (
        "compose-smoke must curl the published port from the runner; the "
        "container's own healthcheck only proves the process is up inside its "
        "namespace, and reports healthy even when the port refuses connections")


def test_lint_installs_ruff_from_the_pin_file() -> None:
    body = CI.read_text(encoding="utf-8")
    literal = re.search(r"ruff==([0-9.]+)", body)
    assert not literal, (
        f"ci.yml pins ruff=={literal.group(1)} inline while requirements-dev.txt "
        "carries its own pin. Two pins for one tool drift, and the failure mode "
        "is CI green / local red. Install from requirements-dev.txt.")


def test_release_publishes_every_shipped_image() -> None:
    body = RELEASE.read_text(encoding="utf-8")
    offenders = [
        rel for rel in _tracked("deploy/*Dockerfile*")
        if f"file: {rel}" not in body
    ]
    assert not offenders, (
        "these images ship in a compose profile but no release builds or signs "
        "them, so anyone using that profile builds an unverified image locally — "
        f"{offenders}")
