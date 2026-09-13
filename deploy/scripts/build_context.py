"""Create a reviewable Docker context from tracked source and explicit private code.

Public builds never copy untracked files. Private additions are source directories
the owner selects deliberately, not discovery from the current developer's home.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parents[2]
_SOURCE_SUFFIXES = {".py", ".pyi", ".ts", ".tsx", ".js", ".mjs", ".json", ".css",
                    ".yaml", ".yml", ".md", ".txt", ".sh", ".html", ".svg"}


def create(destination: Path, *, source: Path = SOURCE, additions: dict | None = None) -> dict:
    target, source = destination.resolve(), source.resolve()
    if target.exists() or target.is_relative_to(source):
        raise ValueError("Build context must be a new directory outside the checkout")
    files = subprocess.run(["git", "ls-files", "-z"], cwd=source, check=True,
                           capture_output=True).stdout.decode().split("\0")
    target.mkdir(parents=True, mode=0o700)
    try:
        for name in filter(None, files):
            origin = source / name
            if origin.is_symlink() or not origin.resolve().is_relative_to(source):
                raise ValueError("Build source may not contain symlinks")
            if not origin.is_file():
                continue  # a tracked deletion in the working tree
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, dest)
        included = []
        for label, folder in (additions or {}).items():
            if label not in ("frontend/src/overlay", "overlay/ava_bridge", "overlay/agent"):
                raise ValueError("Unsupported private build destination")
            folder = Path(folder).resolve()
            if not folder.is_dir():
                raise ValueError("Private source directory does not exist")
            for origin in folder.rglob("*"):
                if origin.is_symlink():
                    raise ValueError("Private build source may not contain symlinks")
                if not origin.is_file():
                    continue
                relative = origin.relative_to(folder)
                if any(part.startswith(".") or part in ("node_modules", "__pycache__", "secrets") for part in relative.parts):
                    continue
                if origin.suffix not in _SOURCE_SUFFIXES:
                    raise ValueError(f"Private build contains a non-source file: {relative}")
                dest = target / label / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, dest)
            included.append(label)
        # Explicit additions override only the relevant directory exclusions.
        # Secret, database, backup and environment exclusions still apply.
        ignore = target / ".dockerignore"
        lines = ignore.read_text(encoding="utf-8").splitlines()
        remove = set(included)
        if any(name.startswith("overlay/") for name in included):
            remove.add("overlay")
        lines = [line for line in lines if line.lstrip("/") not in remove]
        ignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True,
                                  capture_output=True, text=True).stdout.strip()
        record = {"format": "ava-build-context/1", "revision": revision,
                  "private": bool(included), "additions": included}
        (target / "build-context.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record
    except Exception:
        shutil.rmtree(target)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frontend-extensions", type=Path)
    parser.add_argument("--backend-extensions", type=Path)
    parser.add_argument("--agent-extensions", type=Path)
    args = parser.parse_args()
    additions = {label: value for label, value in (
        ("frontend/src/overlay", args.frontend_extensions),
        ("overlay/ava_bridge", args.backend_extensions),
        ("overlay/agent", args.agent_extensions)) if value}
    print(json.dumps(create(args.output, additions=additions), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
