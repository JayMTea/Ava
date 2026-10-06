"""The one verification command. Run it after every change; CI runs exactly this.

    python scripts/check.py            # hygiene, validate config, lint, tests, offline evals
    python scripts/check.py --fast     # hygiene, validate config, tests

Exit code 0 means the platform is consistent and every offline check passes.

In the template repository (where .template/ exists) it also validates and evaluates the
worked example in examples/reference and runs the template's own tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFLICT_SUFFIX = ".from-template"
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
# Content that belongs to the template repository only. A project that contains it was
# copied by hand instead of with .template/new_project.py.
TEMPLATE_ONLY = [".template", "examples/reference/agent.yaml"]


def step(title: str, argv: list[str]) -> bool:
    print(f"\n== {title}: {' '.join(argv)}", flush=True)
    ok = subprocess.run(argv, cwd=ROOT, check=False).returncode == 0
    print(f"-- {title}: {'ok' if ok else 'FAILED'}", flush=True)
    return ok


def is_template() -> bool:
    return (ROOT / ".template").is_dir() and not adopted()


def adopted() -> bool:
    manifest = yaml.safe_load((ROOT / "agent.yaml").read_text(encoding="utf-8")) or {}
    return bool((manifest.get("template") or {}).get("adopted_at"))


def hygiene() -> bool:
    """Template residue and unmerged files - the guarantees a copy can silently break."""
    print("\n== template hygiene", flush=True)
    problems: list[str] = []
    warnings: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(CONFLICT_SUFFIX):
                rel = (Path(dirpath) / name).relative_to(ROOT).as_posix()
                problems.append(f"unmerged template file {rel}: merge it into {rel.removesuffix(CONFLICT_SUFFIX)}, then delete it")
    if adopted():
        for rel in TEMPLATE_ONLY:
            if (ROOT / rel).exists():
                problems.append(f"template-only content in a project: remove {rel.split('/agent.yaml')[0]}/")
        placeholder_files = [ROOT / "AGENT.md", *sorted((ROOT / "agents").glob("*/agent.yaml")), *sorted((ROOT / "agents").glob("*/instructions.md"))]
        for path in placeholder_files:
            if "(placeholder)" in path.read_text(encoding="utf-8"):
                warnings.append(f"{path.relative_to(ROOT).as_posix()} still contains template placeholder text")
    for w in warnings:
        print(f"warning: {w}")
    for p in problems:
        print(f"error: {p}")
    print(f"-- template hygiene: {'ok' if not problems else 'FAILED'}", flush=True)
    return not problems


def main() -> int:
    fast = "--fast" in sys.argv
    py = sys.executable
    results = {"hygiene": hygiene(), "validate": step("validate config", [py, "-m", "app.cli", "validate"])}
    if not fast:
        if _has_module("ruff"):
            results["lint"] = step("lint", [py, "-m", "ruff", "check", "."])
        elif shutil.which("ruff"):
            results["lint"] = step("lint", ["ruff", "check", "."])
        else:
            print("\n== lint: skipped (ruff not installed; pip install -e '.[dev]')")
    results["tests"] = step("tests", [py, "-m", "pytest"])
    if not fast:
        results["evals"] = step("offline evals", [py, "-m", "evals.run", "--route", "test"])
    if is_template():
        results["example: validate"] = step("example: validate", [py, "-m", "app.cli", "--root", "examples/reference", "validate"])
        results["template tests"] = step("template tests", [py, "-m", "pytest", ".template/tests"])
        if not fast:
            results["example: evals"] = step("example: evals", [py, "-m", "evals.run", "--root", "examples/reference", "--route", "test"])
    failed = [name for name, ok in results.items() if not ok]
    print(f"\n{'ALL CHECKS PASSED' if not failed else 'FAILED: ' + ', '.join(failed)}")
    return 1 if failed else 0


def _has_module(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


if __name__ == "__main__":
    sys.exit(main())
