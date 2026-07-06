"""Ava settings — the portable configuration foundation.

Layered resolution (highest wins):
    1. environment variables      (e.g. AVA_PORT)
    2. $AVA_HOME/ava.yaml          (the user's config file)
    3. built-in defaults

Plus a single **data root** (`AVA_HOME`) under which all runtime state lives
(config, data, logs, media, models, secrets). This is the piece that makes Ava
portable: nothing in the code needs to know it lives at /home/<you>/projects/Ava.

**Backwards compatible on purpose:** with no `ava.yaml` and no `AVA_HOME` set,
`AVA_HOME` defaults to the repo root, so the existing personal install keeps
resolving every path exactly as before. New/packaged installs just set
`AVA_HOME=/data` (or `~/.ava`) and everything relocates cleanly.

See docs/PACKAGING_PLAN.md.
"""
from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None

# Where the *code* lives (repo root = parent of this package). Used for
# self-editing / packaged image paths — never for user data.
CODE_ROOT = Path(__file__).resolve().parent.parent

# Where *data* lives. Override with AVA_HOME; defaults to the code root so the
# original single-user layout (./data ./logs ./media) is unchanged.
AVA_HOME = Path(os.environ.get("AVA_HOME", str(CODE_ROOT))).expanduser()

CONFIG_PATH = AVA_HOME / "ava.yaml"


def _load_config() -> dict:
    if yaml is not None and CONFIG_PATH.is_file():
        try:
            return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — a broken config must not crash boot
            return {}
    return {}


_CFG = _load_config()


def _dig(d: dict, dotted: str):
    cur = d
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def get(dotted: str, default=None, env: str | None = None):
    """Resolve a setting: env override -> ava.yaml (dotted path) -> default."""
    if env:
        v = os.environ.get(env)
        if v is not None:
            return v
    v = _dig(_CFG, dotted)
    return v if v is not None else default


def get_int(dotted: str, default: int, env: str | None = None) -> int:
    try:
        return int(get(dotted, default, env))
    except (TypeError, ValueError):
        return default


def get_bool(dotted: str, default: bool, env: str | None = None) -> bool:
    v = get(dotted, None, env)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


# ---- identity (brand + owner) — the re-branding seam ---------------------- #
# These drive the assistant's name, who it serves, and its persona. A fork
# re-brands entirely from ava.yaml/env; the code names no person or place.
def brand_name() -> str:
    return (get("brand.name", "Ava", env="AVA_NAME") or "Ava").strip()


def owner_name() -> str:
    """Who the assistant serves. Empty = a neutral 'the user'."""
    return (get("owner.name", "", env="AVA_OWNER_NAME") or "").strip()


def owner_location() -> str:
    """Default place for weather/local/'near me' questions. Empty = ask."""
    return (get("owner.location", "", env="AVA_OWNER_LOCATION") or "").strip()


def owner_hardware() -> str:
    return (get("owner.hardware", "", env="AVA_OWNER_HARDWARE")
            or "your local hardware").strip()


# ---- standard directories (all under AVA_HOME) ---------------------------- #
def home(*parts: str) -> str:
    p = AVA_HOME.joinpath(*parts)
    return str(p)


def ensure_dirs() -> None:
    for d in (data_dir(), logs_dir(), media_dir(), upload_dir(), secrets_dir()):
        os.makedirs(d, exist_ok=True)


def data_dir() -> str:
    return get("paths.data", home("data"), env="AVA_DATA_DIR")


def logs_dir() -> str:
    return get("paths.logs", home("logs"), env="AVA_LOGS_DIR")


def media_dir() -> str:
    return get("paths.media", home("media", "gen"), env="AVA_MEDIA_DIR")


def upload_dir() -> str:
    return get("paths.uploads", home("media", "uploads"), env="AVA_UPLOAD_DIR")


def secrets_dir() -> str:
    return get("paths.secrets", home("secrets"), env="AVA_SECRETS_DIR")


def models_dir() -> str:
    """Root for downloaded model weights (hf/, ollama/, gpusvc/). Matches the
    Docker volume layout ($AVA_HOME/models/*) so bare-metal and container installs
    share one shape."""
    return get("paths.models", home("models"), env="AVA_MODELS_DIR")


# ---- secrets -------------------------------------------------------------- #
def secret(name: str, env: str | None = None, generate: bool = False,
           nbytes: int = 32) -> str | None:
    """Resolve a secret: env -> $AVA_HOME/secrets/<name> -> (optionally) generate.

    Generated secrets are written 0600 so a fresh install is secure by default
    without the user having to invent one.
    """
    if env:
        v = os.environ.get(env)
        if v:
            return v
    path = Path(secrets_dir()) / name
    if path.is_file():
        try:
            val = path.read_text(encoding="utf-8").strip()
            if val:
                return val
        except OSError:
            pass
    if generate:
        os.makedirs(secrets_dir(), exist_ok=True)
        val = _secrets.token_urlsafe(nbytes)
        try:
            path.write_text(val, encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:
            pass
        return val
    return None


def as_dict() -> dict:
    """The loaded ava.yaml (for `ava doctor` / debugging)."""
    return dict(_CFG)
