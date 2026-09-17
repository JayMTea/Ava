"""Public-tree privacy checks. Instance identifiers stay in Git's local info directory.

Scan every UTF-8 file, including extensionless metadata and generated text. Binary
media needs separate visual review. These checks cannot erase Git history.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_FORBIDDEN = [
    (re.compile("UNLICENSED" + " COPY"), "unlicensed layout watermark"),
    (re.compile(r"/home/[a-z][a-z0-9_-]*/|/Users/[a-z][a-z0-9_-]*/", re.I),
     "absolute personal home path; use settings or a synthetic /srv/example path"),
    (re.compile(r"[A-Z]:[\\/]+Users[\\/]+[a-z][a-z0-9_.-]*", re.I),
     "absolute Windows user path; use settings or a synthetic path"),
    (re.compile(r"linkedin\.com/in/|[\w+.-]+@users\.noreply\.github\.com", re.I),
     "personal profile or inbox; use project contact metadata"),
    (re.compile(r"\bspark-[0-9a-f]{4}\b", re.I),
     "deployment hostname; use an example hostname"),
    (re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
     "literal CGNAT address; use a configured hostname"),
]
# Exceptions apply ONLY to generic rules, never to instance-private names.
_ALLOW = {
    "tests/test_no_owner_identity.py",
    "ava_security_check.py",             # Defines the public CGNAT network range.
    "tests/test_port_exposure_classes.py",  # Tests that range and its boundaries.
}
_MODEL_SUFFIXES = {".gguf", ".safetensors", ".pt", ".pth", ".onnx", ".npy", ".npz", ".pkl"}
_PRIVATE_ROOTS = {
    "data", "logs", "media", "models", "enroll", "secrets", "overlay",
    "extensions", "runtime_adapters", "alloc_drivers", "backups", "run",
}
_PUBLIC_TEMPLATES = {
    "data/.gitkeep", "logs/.gitkeep", "media/uploads/.gitkeep",
    "enroll/enrollment_script.txt",  # Generic reading prompt, never a recording.
}


def _git_info_dir() -> pathlib.Path:
    out = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT,
                         capture_output=True, text=True, check=True, timeout=10)
    path = pathlib.Path(out.stdout.strip())
    return (path if path.is_absolute() else ROOT / path).resolve() / "info"


_PRIVATE_NAMES_FILE = _git_info_dir() / "private-names"


def _private_patterns() -> list[tuple[re.Pattern, str]]:
    raw = os.environ.get("AVA_PRIVATE_NAMES", "")
    if _PRIVATE_NAMES_FILE.exists():
        # Unreadable rules must fail, rather than silently disable the check.
        raw += "\n" + _PRIVATE_NAMES_FILE.read_text(encoding="utf-8")
    rules = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            rules.append((re.compile(line.strip(), re.I),
                          "private identifier (local/CI rules; match redacted)"))
        except re.error:
            raise AssertionError(f"Invalid private-name regex on line {number}") from None
    return rules


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout
    return sorted(set(out.decode("utf-8").strip("\0").split("\0")) - {""})


def _tracked_text(rel: str) -> str | None:
    try:
        data = (ROOT / rel).read_bytes()
    except OSError:
        # A locally deleted but unstaged file still ships from HEAD.
        out = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT,
                             capture_output=True, timeout=20)
        if out.returncode:
            return None
        data = out.stdout
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan() -> list[str]:
    offenders = []
    private = _private_patterns()
    for rel in _tracked_files():
        path = pathlib.PurePosixPath(rel)
        if ((path.parts[0] in _PRIVATE_ROOTS and rel not in _PUBLIC_TEMPLATES)
                or path.suffix.lower() in _MODEL_SUFFIXES):
            offenders.append(f"{rel}: instance data or model artifact must not ship")
        rules = private + ([] if rel in _ALLOW else _FORBIDDEN)
        body = _tracked_text(rel)
        for pattern, reason in rules:
            if pattern.search(rel) or (body is not None and pattern.search(body)):
                offenders.append(f"{rel}: {reason}")
    return sorted(set(offenders))


def test_the_private_names_guard_is_not_silently_disabled():
    if not _PRIVATE_NAMES_FILE.exists() and not os.environ.get("AVA_PRIVATE_NAMES"):
        pytest.skip("No local/CI private-name list; only generic privacy checks run")
    assert _private_patterns(), "Private-name input exists but contains no patterns"


def test_no_tracked_file_carries_owner_identity():
    assert not (offenders := _scan()), "Public source privacy violations:\n" + "\n".join(offenders)


def test_private_rules_cover_metadata_and_generic_exemptions(tmp_path, monkeypatch):
    rulefile = tmp_path / "rules"
    rulefile.write_text("private-example-marker\n", encoding="utf-8")
    monkeypatch.setattr(__import__(__name__), "_PRIVATE_NAMES_FILE", rulefile)
    monkeypatch.setenv("AVA_PRIVATE_NAMES", "")
    monkeypatch.setattr(__import__(__name__), "_tracked_files",
                        lambda: ["NOTICE", "CITATION.cff", "sample.custom", "ava_security_check.py"])
    monkeypatch.setattr(__import__(__name__), "_tracked_text",
                        lambda rel: "private-example-marker")
    assert len(_scan()) == 4


def test_private_paths_and_model_artifacts_are_checked(monkeypatch):
    module = __import__(__name__)
    monkeypatch.setattr(module, "_private_patterns", lambda: [])
    monkeypatch.setattr(module, "_tracked_files",
                        lambda: ["data/chats.db", "download/checkpoint.gguf"])
    monkeypatch.setattr(module, "_tracked_text", lambda rel: None)
    assert len(_scan()) == 2


def test_topology_diagrams_are_not_tracked():
    forbidden = {f"agent/docs/diagrams/{name}.svg" for name in ("system", "network", "policy")}
    assert not forbidden.intersection(_tracked_files()), "Keep deployment topology outside public source"
