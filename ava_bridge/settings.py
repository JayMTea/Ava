"""Ava settings — the portable configuration foundation.

Layered resolution (highest wins):
    1. environment variables      (e.g. AVA_PORT)
       (a `.env` file at the repo root or $AVA_HOME is auto-loaded into the
        environment at import — values already present in the real environment
        always win, so systemd EnvironmentFile=/compose `environment:` keep
        priority)
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


def _load_dotenv(path: Path) -> None:
    """Minimal stdlib .env loader (KEY=value, `#` comments, optional `export `,
    single/double quotes; $VAR expansion except inside single quotes).

    Uses os.environ.setdefault so anything already in the real environment
    (systemd EnvironmentFile=, docker compose `environment:`, shell exports,
    the run scripts' own `set -a; . .env`) always wins.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or any(c in key for c in " \t"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
            value = value[1:-1]
        else:
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            value = os.path.expandvars(value)
        os.environ.setdefault(key, value)


# The repo .env loads first (it may define AVA_HOME itself), then AVA_HOME is
# resolved, then $AVA_HOME/.env (if different) fills any remaining gaps.
_load_dotenv(CODE_ROOT / ".env")

# Where *data* lives. Override with AVA_HOME; defaults to the code root so the
# original single-user layout (./data ./logs ./media) is unchanged.
AVA_HOME = Path(os.environ.get("AVA_HOME", str(CODE_ROOT))).expanduser()

if AVA_HOME.resolve() != CODE_ROOT.resolve():
    _load_dotenv(AVA_HOME / ".env")

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


# How long accumulated telemetry/history (perf rollups, hardware samples) is
# retained. One knob, surfaced in Setup → System. Default ~6 months.
DATA_RETENTION_DEFAULT_DAYS = 183
# Allowed choices the UI offers (days); 0 == keep forever.
DATA_RETENTION_CHOICES = (30, 90, 183, 365, 730, 0)


def data_retention_days() -> int:
    """Configured retention in days (0 == forever). `data.retention_days`."""
    return get_int("data.retention_days", DATA_RETENTION_DEFAULT_DAYS,
                   env="AVA_DATA_RETENTION_DAYS")


def data_retention_s() -> float:
    """Configured retention in seconds (0 == forever)."""
    days = data_retention_days()
    return 0.0 if days <= 0 else float(days) * 86400


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


def _deep_merge(base: dict, patch: dict) -> dict:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def save_patch(patch: dict) -> dict:
    """Deep-merge `patch` into $AVA_HOME/ava.yaml and persist it (the single
    write path for the setup wizard and any future `ava config set`). Seeds from
    config.example.yaml on first write, refreshes the in-process config, and
    returns the merged config. Requires PyYAML."""
    if yaml is None:
        raise RuntimeError("PyYAML is required to write ava.yaml")
    current: dict = {}
    if CONFIG_PATH.is_file():
        try:
            current = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            current = {}
    elif (CODE_ROOT / "config.example.yaml").is_file():
        try:
            current = yaml.safe_load(
                (CODE_ROOT / "config.example.yaml").read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            current = {}
    merged = _deep_merge(current if isinstance(current, dict) else {}, patch)
    os.makedirs(CONFIG_PATH.parent, exist_ok=True)
    CONFIG_PATH.write_text(
        yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
    global _CFG
    _CFG = merged
    return merged


def role_backend(role: str) -> str | None:
    """Resolve a logical model role (`inference.roles.<role>`: chat / fast /
    embed / …) to a declared backend id, falling back to `inference.primary`.
    Returns None when neither is configured (legacy/default installs)."""
    v = get(f"inference.roles.{role}")
    if v:
        return str(v)
    v = get("inference.primary")
    return str(v) if v else None


def as_dict() -> dict:
    """The loaded ava.yaml (for `ava doctor` / debugging)."""
    return dict(_CFG)
