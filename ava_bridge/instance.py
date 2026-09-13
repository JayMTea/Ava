"""Instance inventory, consistent backups and restore into a new directory.

Archives contain confidential instance data and credentials. They are created
owner-readable and must be stored like the instance itself. Model weights are
excluded; enrollment is included. Running services are never changed here.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile
import secrets

from . import settings

FORMAT = "ava-instance/1"
_ROOT_FILES = ("ava.yaml", ".env", "domains.yaml", "connector_grants.yaml",
               "connector_tools_cache.json")


def roots() -> dict[str, Path]:
    return {"data": Path(settings.data_dir()), "logs": Path(settings.logs_dir()),
            "secrets": Path(settings.secrets_dir()), "media/uploads": Path(settings.upload_dir()),
            "branding": Path(settings.brand_dir()), "connectors": Path(settings.home("connectors")),
            "agent": Path(settings.agent_state_dir()), "overlay": Path(settings.home("overlay")),
            "overlay/agent": Path(settings.overlay_dir()),
            "extensions": Path(settings.home("extensions")),
            "runtime_adapters": Path(settings.home("runtime_adapters")),
            "alloc_drivers": Path(settings.home("alloc_drivers"))}


def inspect() -> dict:
    """Locations and counts only. Never expose configuration or credential values."""
    from . import connectors, runtime
    return {"format": FORMAT, "home": str(settings.AVA_HOME),
            "runtime": runtime.configured().name,
            "connectors": [{"id": m["id"], "enabled": m.get("enabled", True),
                            "transport": connectors.transport(m)} for m in connectors.catalog()],
            "roots": {name: {"path": str(path), "exists": path.exists()}
                      for name, path in roots().items()},
            "configuration_present": settings.CONFIG_PATH.is_file()}


def backup(destination: str) -> dict:
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise ValueError("Backup destination already exists; choose a new filename")
    source_roots = roots()
    for root in source_roots.values():
        if target.is_relative_to(root.resolve()):
            raise ValueError("Place the archive outside the state directories being backed up")
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"format": FORMAT, "files": {}, "model_weights_included": False}
    entries: dict[str, Path] = {}
    for name in _ROOT_FILES:
        path = Path(settings.home(name))
        if path.is_file() and not path.is_symlink():
            entries[name] = path
    for label, root in source_roots.items():
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(root)
                if any(part in ("__pycache__", ".git", "node_modules") for part in relative.parts):
                    continue
                if path.name.endswith(("-wal", "-shm", ".lock")):
                    continue
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("State file escapes its configured root")
                entries[f"{label}/{relative.as_posix()}"] = path
    voice = Path(settings.models_dir()) / "voiceprint.npy"
    if voice.is_file() and not voice.is_symlink():
        entries["models/voiceprint.npy"] = voice
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            with tempfile.TemporaryDirectory(prefix="ava-backup-") as scratch:
                for name, source in sorted(entries.items()):
                    selected = source
                    if source.suffix in (".db", ".sqlite", ".sqlite3"):
                        selected = Path(scratch) / "snapshot.db"
                        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as origin:
                            with closing(sqlite3.connect(selected)) as snapshot:
                                origin.backup(snapshot)
                                if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                                    raise ValueError(f"Database check failed: {name}")
                    digest, size = hashlib.sha256(), 0
                    with selected.open("rb") as source_file, archive.open(name, "w", force_zip64=True) as member:
                        while body := source_file.read(1024 * 1024):
                            member.write(body)
                            digest.update(body)
                            size += len(body)
                    manifest["files"][name] = {"bytes": size, "sha256": digest.hexdigest()}
                archive.writestr("instance-manifest.json", json.dumps(manifest, sort_keys=True))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"ok": True, "archive": str(target), "files": len(entries), "format": FORMAT}


def restore(archive_path: str, destination: str) -> dict:
    """Validate the entire archive before publishing a new, isolated instance."""
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise ValueError("Restore requires a new directory; existing instances are never overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        if len(archive.infolist()) > 100000 or archive.getinfo("instance-manifest.json").file_size > 16 * 1024**2:
            raise ValueError("Backup manifest exceeds the restore limit")
        manifest = json.loads(archive.read("instance-manifest.json"))
        if manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), dict):
            raise ValueError("Unsupported instance backup")
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest["files"]) | {"instance-manifest.json"}:
            raise ValueError("Archive members differ from the manifest")
        if sum(entry.file_size for entry in archive.infolist()) > 20 * 1024**3:
            raise ValueError("Backup exceeds the 20 GiB restore limit")
        for name, expected in manifest["files"].items():
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
                    or not path.parts or str(path) != name):
                raise ValueError("Unsafe archive path")
            if not isinstance(expected, dict) or not isinstance(expected.get("bytes"), int):
                raise ValueError("Invalid file metadata in backup")
            digest, size = hashlib.sha256(), 0
            with archive.open(name) as member:
                while body := member.read(1024 * 1024):
                    digest.update(body)
                    size += len(body)
            if size != expected["bytes"] or digest.hexdigest() != expected.get("sha256"):
                raise ValueError(f"Backup checksum mismatch: {name}")
        staging = Path(tempfile.mkdtemp(prefix=".ava-restore-", dir=target.parent))
        try:
            for name in manifest["files"]:
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "wb", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
                    with archive.open(name) as member:
                        shutil.copyfileobj(member, output, 1024 * 1024)
            # Paths belong to the new home, not to the machine that made the backup.
            import yaml
            config = staging / "ava.yaml"
            document = yaml.safe_load(config.read_text(encoding="utf-8")) if config.is_file() else {}
            document = document or {}
            if not isinstance(document, dict):
                raise ValueError("Backed-up configuration must be a mapping")
            document.pop("paths", None)
            if isinstance(document.get("extensions"), dict):
                document["extensions"].pop("agent_dir", None)
                document["extensions"].pop("legacy_routes", None)
            if isinstance(document.get("voice"), dict):
                document["voice"].pop("legacy_enrollment", None)
            config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            env = staging / ".env"
            if env.is_file():
                path_keys = {"AVA_HOME", "AVA_DATA_DIR", "AVA_LOGS_DIR", "AVA_UPLOAD_DIR",
                             "AVA_MODELS_DIR", "AVA_SECRETS_DIR", "AVA_AGENT_STATE_DIR", "AVA_OVERLAY",
                             "AVA_BRAND_DIR", "AVA_LOAD_REPO_ENV", "AVA_LEGACY_ROUTES", "AVA_LEGACY_VOICEPRINT",
                             "AVA_SECRET"}
                lines = [line for line in env.read_text(encoding="utf-8").splitlines()
                         if line.strip().removeprefix("export ").partition("=")[0].strip() not in path_keys]
                env.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.chmod(config, 0o600)
            # A restored home keeps its password, but sessions from the source
            # instance must not authorize requests to the new instance.
            signing_key = staging / "data" / ".secret"
            signing_key.parent.mkdir(parents=True, exist_ok=True)
            with open(signing_key, "wb", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
                output.write(secrets.token_bytes(32))
            # Rename is atomic on this filesystem. Never merge with an existing home.
            if target.exists():
                raise ValueError("Restore destination appeared during validation")
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging)
            raise
    return {"ok": True, "home": str(target), "files": len(manifest["files"]),
            "next": "Set AVA_HOME to this directory; review external endpoints before starting Ava"}


def adopt(source_path: str, kind: str) -> dict:
    """Copy explicitly selected legacy state; never move it or replace a target."""
    source = Path(source_path).expanduser().resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Select an existing, non-empty source file")
    if kind == "adopt-voiceprint":
        import numpy as np
        if source.stat().st_size > 65536:
            raise ValueError("Enrollment exceeds the supported size")
        embedding = np.load(source, allow_pickle=False)
        if (embedding.shape != (192,) or embedding.dtype.kind != "f"
                or not np.isfinite(embedding).all() or not np.linalg.norm(embedding) > 0):
            raise ValueError("Expected a finite 192-dimensional ECAPA enrollment")
        target = Path(settings.models_dir()) / "voiceprint.npy"
        content = source.read_bytes()
    elif kind == "adopt-artifacts":
        from . import data_artifacts
        target = Path(data_artifacts.db_path())
        with tempfile.TemporaryDirectory(prefix="ava-adopt-") as scratch:
            snapshot = Path(scratch) / "artifacts.db"
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as origin:
                columns = {row[1] for row in origin.execute("PRAGMA table_info(artifact)")}
                if columns != {"id", "connector", "created", "reference", "payload"}:
                    raise ValueError("Source is not an Ava artifact database")
                if origin.execute("PRAGMA user_version").fetchone()[0] > 1:
                    raise ValueError("Source requires a newer Ava version")
                with closing(sqlite3.connect(snapshot)) as destination:
                    origin.backup(destination)
                    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("Source database failed its integrity check")
            content = snapshot.read_bytes()
    else:
        raise ValueError("Unknown legacy state type")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
            output.write(content)
    except FileExistsError as exc:
        raise ValueError("The selected instance already has this state; nothing was replaced") from exc
    from . import audit
    audit.record("instance_adopt", store=kind.removeprefix("adopt-"),
                 sha256=hashlib.sha256(content).hexdigest(), source_preserved=True)
    return {"ok": True, "path": str(target), "source_preserved": True}


def command(args) -> int:
    try:
        if args.action == "inspect":
            result = inspect()
        elif args.action == "backup":
            if not args.output:
                raise ValueError("backup requires --output")
            result = backup(args.output)
        elif args.action == "restore":
            if not args.source or not args.target:
                raise ValueError("restore requires --source and --target")
            result = restore(args.source, args.target)
        elif args.action in ("adopt-voiceprint", "adopt-artifacts"):
            if not args.source:
                raise ValueError("adoption requires --source")
            result = adopt(args.source, args.action)
        else:
            raise ValueError("Unknown instance operation")
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
