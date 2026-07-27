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

import copy as _copy
import os
import re as _re
import secrets as _secrets
import shutil as _shutil
import sys as _sys
import tempfile as _tempfile
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


class ConfigParseError(RuntimeError):
    """ava.yaml exists but does not parse. Raised by the write paths."""


def _load_config() -> tuple[dict, str]:
    """(config, error). A broken ava.yaml must not crash boot — but it must not
    be INVISIBLE either, which is what returning a bare {} made it.

    Silent {} meant a broken config was indistinguishable from no config, so
    every `get()` quietly returned its default. On a box running the allocator
    with `evict: true`, one bad indent silently reverted it to advisory.
    """
    if yaml is None or not CONFIG_PATH.is_file():
        return {}, ""
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as e:
        return {}, f"could not read {CONFIG_PATH}: {e}"
    try:
        loaded = yaml.safe_load(raw)
    except Exception as e:  # noqa: BLE001 — any YAML error, reported not swallowed
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(e, "problem", None) or str(e).splitlines()[0]
        return {}, f"{problem}{where}"
    if loaded is None:
        return {}, ""
    if not isinstance(loaded, dict):
        return {}, f"expected a mapping at the top level, got {type(loaded).__name__}"
    return loaded, ""


_CFG, _CFG_ERROR = _load_config()

if _CFG_ERROR:
    # Printed once, at import, because the alternative is a user staring at
    # settings that "did not take effect" with nothing anywhere saying why.
    print(f"[ava] WARNING: {CONFIG_PATH} did not parse — {_CFG_ERROR}", file=_sys.stderr)
    print("[ava]   Running on defaults. Config WRITES are refused until it is "
          "fixed, so nothing overwrites what is still in the file.", file=_sys.stderr)


def load_error() -> str:
    """The current ava.yaml parse error, or "" when it is fine.

    Mirrors connectors.load_errors() / skills.load_errors(), which already solve
    "a config file the user wrote is broken and they need to be told" correctly.
    """
    return _CFG_ERROR


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


def get_float(dotted: str, default: float, env: str | None = None) -> float:
    """The missing member of the get_int/get_bool family.

    Without it, callers wrote a bare `float(get(...))` — and ava_bridge/config.py
    did exactly that for `voice.threshold` at import, so a non-numeric value
    there raised ValueError while importing config and the bridge did not start,
    with no line of output naming the setting responsible.
    """
    try:
        return float(get(dotted, default, env))
    except (TypeError, ValueError):
        return default


def get_bool(dotted: str, default: bool, env: str | None = None) -> bool:
    v = get(dotted, None, env)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def env_override(env: str) -> str | None:
    """The env var NAME when it is currently set (and therefore shadows the
    ava.yaml key an editor writes), else None. Editors surface this so "I saved
    it but it reverted after restart" has a visible cause instead of being a
    silent env-beats-yaml mystery."""
    return env if os.environ.get(env) not in (None, "") else None


def explicitly_false(dotted: str, env: str | None = None) -> bool:
    """True only when the user EXPLICITLY turned a flag off (yaml false / env
    0|false|no|off). Unset is NOT off — runtime gates use this so installs that
    never wrote the key keep working, while a deliberate "off" is honored
    instead of being decorative."""
    v = get(dotted, None, env)
    if v is None:
        return False
    return str(v).strip().lower() in ("0", "false", "no", "off")


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
    # alloc_drivers/ is the documented drop-in point for a third-party engine
    # adapter (docs/ALLOCATION.md). It was never created and no doc said to
    # mkdir it, so the first step of "add support for your engine" was a
    # directory that did not exist.
    for d in (data_dir(), logs_dir(), media_dir(), upload_dir(), secrets_dir(),
              home("alloc_drivers")):
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


def _safe_backend_id(bid: str) -> str:
    """Filesystem-safe form of a user-supplied backend id (secrets filename)."""
    return _re.sub(r"[^a-z0-9_-]", "", (bid or "").strip().lower())[:48]


def backend_key(backend_id: str) -> str | None:
    """Per-backend API key from the secrets store (`secrets/<id>.key`), written by
    the Hub's model manager and resolved by router_app.load_backends. Keeps cloud
    keys out of ava.yaml while still reaching the router. None if absent/empty."""
    bid = _safe_backend_id(backend_id)
    if not bid:
        return None
    p = Path(secrets_dir()) / f"{bid}.key"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def write_backend_key(backend_id: str, key: str) -> None:
    """Store a per-backend API key 0600 under `secrets/<id>.key`."""
    bid = _safe_backend_id(backend_id)
    if not bid:
        return
    os.makedirs(secrets_dir(), exist_ok=True)
    p = Path(secrets_dir()) / f"{bid}.key"
    p.write_text(key, encoding="utf-8")
    os.chmod(p, 0o600)


def delete_backend_key(backend_id: str) -> None:
    """Remove a per-backend key file if present (best-effort)."""
    bid = _safe_backend_id(backend_id)
    if not bid:
        return
    p = Path(secrets_dir()) / f"{bid}.key"
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


# ---- connector credentials (env-var-named secrets) ------------------------ #
# A connector manifest references its app's credential by the NAME of an env var
# (`auth: { token_env: MYAPP_TOKEN }`), never the value. The value can come from
# a real environment variable (systemd/compose/shell — which always win) OR be
# saved once from the Setup Hub under $AVA_HOME/secrets/env/<NAME> (0600). This
# is the store that makes fork-and-self-host painless: the owner pastes the token
# once in the UI and never edits `.env`, and it survives every restart + redeploy.
#
# INVARIANT (Ava-never-has-passwords): the value is only ever READ here, on the
# bridge side, when building an egress request. It is never placed into
# os.environ globally, so no spawned subprocess — including the sandboxed agent
# runtime — inherits it, and it is never written to the manifest or ava.yaml.
def _safe_env_name(name: str) -> str:
    """Filesystem-safe form of an env-var name used as a secrets filename. Env
    names are ``[A-Za-z_][A-Za-z0-9_]*``; keep the case, strip anything else."""
    return _re.sub(r"[^A-Za-z0-9_]", "", (name or "").strip())[:64]


def _env_secret_path(name: str) -> Path:
    return Path(secrets_dir()) / "env" / _safe_env_name(name)


def env_secret(name: str) -> str | None:
    """Resolve a connector credential by env-var NAME: a real environment variable
    wins, else the value saved once via the Hub (``secrets/env/<NAME>``). ``None``
    if neither is set. The single read-path for connector auth tokens."""
    if not name:
        return None
    v = os.environ.get(name)
    if v:
        return v
    p = _env_secret_path(name)
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def has_env_secret(name: str) -> bool:
    """True if a credential is available for this env-var name (real env var OR a
    saved secret) — lets the Hub show 'credential saved' and never re-prompt."""
    return bool(env_secret(name))


def env_secret_stored(name: str) -> bool:
    """True if a value is saved in the secrets store specifically (not just a live
    env var) — so the Hub knows there is a file it can update or clear."""
    return bool(name) and _env_secret_path(name).is_file()


def set_env_secret(name: str, value: str) -> None:
    """Persist a connector credential 0600 under ``secrets/env/<NAME>``, keyed by
    the env-var name the manifest references. Written once from the Hub; survives
    restarts and every redeploy. Never written to the manifest or ava.yaml."""
    safe = _safe_env_name(name)
    if not safe or not value:
        return
    d = Path(secrets_dir()) / "env"
    os.makedirs(d, exist_ok=True)
    p = d / safe
    p.write_text(value, encoding="utf-8")
    os.chmod(p, 0o600)


def clear_env_secret(name: str) -> None:
    """Remove a saved connector credential if present (best-effort)."""
    p = _env_secret_path(name)
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def _deep_merge(base: dict, patch: dict) -> dict:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _refuse_if_broken() -> None:
    """Never overwrite a config we could not read.

    This is the important half. `save_patch` used to re-read from disk, swallow
    the parse error into `current = {}`, deep-merge the patch into nothing, and
    write unconditionally — so ONE bad indent plus ONE Setup toggle replaced the
    whole file with just the toggle. Measured: a 13-line ava.yaml became 2 lines,
    taking brand, owner and the entire alloc block with it.
    """
    cfg, err = _load_config()
    if err:
        global _CFG_ERROR
        _CFG_ERROR = err
        raise ConfigParseError(
            f"{CONFIG_PATH} does not parse ({err}). Refusing to write, because "
            "saving now would replace everything still in that file. Fix the "
            f"YAML, or restore the backup at {CONFIG_PATH}.bak.")
    return cfg


def _write_config(cfg: dict) -> None:
    """Atomically replace ava.yaml, keeping one backup.

    Atomic because the previous `write_text` could leave a half-written config on
    a full disk or a crash — and the file it was truncating is the only copy of
    the owner's settings. 0600 because ava.yaml carries the owner's name and
    location, and may carry backend API bases.
    """
    os.makedirs(CONFIG_PATH.parent, exist_ok=True)
    if CONFIG_PATH.is_file():
        try:
            _shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_suffix(".yaml.bak"))
        except OSError:
            pass                        # a missing backup must not block the save
    body = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    fd, tmp = _tempfile.mkstemp(dir=str(CONFIG_PATH.parent),
                                prefix=".ava.yaml.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_patch(patch: dict) -> dict:
    """Deep-merge `patch` into $AVA_HOME/ava.yaml and persist it (the single
    write path for the setup wizard and any future `ava config set`). Refreshes
    the in-process config and returns the merged result. Requires PyYAML.

    Raises ConfigParseError when the existing file is unreadable — see
    _refuse_if_broken.

    NOTE: comments do not survive a save (yaml.safe_dump cannot round-trip them).
    That is why `ava setup` writes a MINIMAL ava.yaml rather than copying the
    annotated config.example.yaml: the template is documentation, and importing
    370 lines of it into the user's file just so the first save could strip them
    was the actual defect. See ava_cli.cmd_setup.
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required to write ava.yaml")
    current = _refuse_if_broken()
    merged = _deep_merge(current if isinstance(current, dict) else {}, patch)
    _write_config(merged)
    global _CFG
    _CFG = merged
    return merged


def current_config() -> dict:
    """A deep copy of the live config for callers that will mutate it and then
    persist with save_config (e.g. deleting a backend — a merge can't express a
    removal)."""
    return _copy.deepcopy(_CFG)


def save_config(cfg: dict) -> dict:
    """Persist a FULL config dict to ava.yaml and refresh the in-process copy.
    The low-level primitive for edits that must delete keys (which save_patch's
    deep-merge cannot). Callers typically start from current_config().

    Refuses on an unparseable file for the same reason save_patch does: callers
    build their argument from current_config(), which on a broken file is `{}` —
    so writing it would delete everything.
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required to write ava.yaml")
    _refuse_if_broken()
    _write_config(cfg)
    global _CFG
    _CFG = cfg
    return cfg


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


# Bridge a UI-saved cloud key (secrets/inference_key, written by the first-run
# wizard / single-backend save) into the env the router + direct chat actually
# read (AVA_INFERENCE_KEY). Without this, a cloud brain configured from the
# browser saves a key that never reaches load_backends. setdefault => a real env
# var or .env entry still wins. Per-backend keys use backend_key() instead.
try:
    _legacy_key = secret("inference_key")
    if _legacy_key:
        os.environ.setdefault("AVA_INFERENCE_KEY", _legacy_key)
except Exception:  # noqa: BLE001 — never let secret probing break boot
    pass
