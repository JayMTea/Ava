"""Every deploy script a tracked file points at must itself be tracked.

`deploy/local-serve.sh` existed on the author's disk for days, was `exec`'d by the
TRACKED `deploy/omni-serve.sh`, and was named by five other tracked files —
deploy/README.md, docs/CHOOSE_A_MODEL.md, config.example.yaml, .env.example and
deploy/docker-compose.yml — while `git ls-files` had never heard of it. A clone
of that tree got a launcher that immediately exec'd a file that did not exist, and
the entire documented "serve your own model on vLLM" path was dead on arrival.

Nothing catches this by running the repo: the file is present on the machine that
wrote the docs, so every local check passes. It is only visible from the index,
which is what this scan reads.

Style matches tests/test_diagram_sync.py: a `git ls-files` scan, no bridge, no
AVA_HOME, no network.
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

# `deploy/<something>.sh` mentioned anywhere in prose, config or compose.
_DEPLOY_SH = re.compile(r"deploy/([A-Za-z0-9_.-]+\.sh)")

# Files that legitimately name a script without it needing to exist.
_ALLOW_REFS = {
    # This file names the pattern it enforces.
    "tests/test_deploy_refs_tracked.py",
    # Release notes describe history, including scripts that have since been
    # renamed or removed. Never rewrite it to satisfy a guard.
    "CHANGELOG.md",
}


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def test_every_referenced_deploy_script_is_tracked() -> None:
    tracked_scripts = set(_tracked("deploy/*.sh"))
    assert tracked_scripts, "no tracked deploy/*.sh found — did deploy/ move?"

    referrers: list[str] = []
    for pattern in ("*.md", "*.yml", "*.yaml", "*.sh", "*.env", ".env.example"):
        referrers += _tracked(pattern)

    offenders: list[str] = []
    for rel in sorted(set(referrers)):
        if rel in _ALLOW_REFS:
            continue
        for name in set(_DEPLOY_SH.findall(_read(rel))):
            if f"deploy/{name}" not in tracked_scripts:
                offenders.append(f"{rel} -> deploy/{name}")

    assert not offenders, (
        "these tracked files reference a deploy script that is NOT in the git "
        "index, so a fresh clone gets a broken path (this is exactly how "
        "deploy/local-serve.sh shipped un-tracked while omni-serve.sh exec'd it): "
        f"run `git add` on the missing script, or fix the reference — {offenders}"
    )


def test_executable_deploy_scripts_are_not_shims_to_nothing() -> None:
    """A tracked script whose only job is to exec another one must name a real
    target. `omni-serve.sh` became a 15-line shim in exactly this shape."""
    offenders: list[str] = []
    for rel in _tracked("deploy/*.sh"):
        for name in set(_DEPLOY_SH.findall(_read(rel))):
            if not (ROOT / "deploy" / name).is_file():
                offenders.append(f"{rel} -> deploy/{name} (not on disk)")
    assert not offenders, (
        "a deploy script points at a sibling that does not exist on disk — "
        f"{offenders}"
    )
