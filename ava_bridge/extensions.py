"""Explicit, versioned installation of trusted instance code.

An extension executes with the bridge's permissions. Connector manifests remain
the preferred integration for untrusted or separately hosted applications.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
import re
import sys

import yaml

from . import settings

API_VERSION = "ava-extension/1"
_ERRORS: list[dict] = []
_ID = re.compile(r"[a-z][a-z0-9_-]{1,63}\Z")


def load_entry(root: Path, entry: str, namespace: str):
    """Load one owner-installed Python entry, refusing traversal and symlinks out."""
    filename, sep, symbol = entry.partition(":")
    base = root.resolve()
    path = (base / filename).resolve()
    if (not sep or not symbol.isidentifier() or path.suffix != ".py"
            or not path.is_relative_to(base) or not path.is_file()):
        raise ValueError("entry must name a Python file inside the extension and a symbol")
    spec = importlib.util.spec_from_file_location(namespace, path, submodule_search_locations=[str(base)])
    if spec is None or spec.loader is None:
        raise ValueError("extension entry cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(namespace, None)
        raise
    return getattr(module, symbol)


def manifest(kind: str, name: str) -> tuple[Path, dict]:
    if kind not in ("extensions", "runtime_adapters") or not _ID.fullmatch(name):
        raise ValueError("invalid extension identifier")
    root = Path(settings.home(kind, name))
    base = Path(settings.home(kind)).resolve()
    if not root.resolve().is_relative_to(base):
        raise ValueError("extension directory escapes the instance")
    document = yaml.safe_load((root / "extension.yaml").read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("api_version") != API_VERSION:
        raise ValueError(f"extension must declare api_version: {API_VERSION}")
    return root, document


def mount(app) -> None:
    """Mount only the extensions explicitly enabled by this instance."""
    _ERRORS.clear()
    enabled = settings.get("extensions.enabled", []) or []
    if not isinstance(enabled, list):
        _ERRORS.append({"id": "configuration", "error": "extensions.enabled must be a list"})
        enabled = []
    for name in enabled:
        try:
            root, doc = manifest("extensions", str(name))
            register = load_entry(root, str(doc.get("backend", "")), f"ava_extension_{name}")
            register(app)
        except Exception as exc:  # noqa: BLE001 — one broken extension cannot prevent login
            # No exception text: extension code may put credentials in its errors.
            _ERRORS.append({"id": str(name), "error": f"extension failed ({type(exc).__name__})"})
            logging.getLogger(__name__).error("Extension %s failed (%s)", name, type(exc).__name__)
    # The original checkout remains a supported legacy instance. A separate
    # AVA_HOME may opt in explicitly; it never inherits this import by accident.
    legacy = settings.get_bool("extensions.legacy_routes", False, env="AVA_LEGACY_ROUTES")
    if legacy or settings.AVA_HOME.resolve() == settings.CODE_ROOT.resolve():
        try:
            from overlay.ava_bridge import personal_routes
            personal_routes.register(app)
        except ModuleNotFoundError as exc:
            if not str(exc.name).startswith("overlay"):
                _ERRORS.append({"id": "legacy", "error": "legacy extension dependency is missing"})
        except Exception as exc:  # noqa: BLE001
            _ERRORS.append({"id": "legacy", "error": f"legacy extension failed ({type(exc).__name__})"})


def status() -> dict:
    return {"api_version": API_VERSION, "enabled": settings.get("extensions.enabled", []) or [],
            "errors": list(_ERRORS)}
