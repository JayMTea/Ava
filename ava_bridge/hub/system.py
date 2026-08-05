"""Setup -> System panel: instance identity, retention, optional features.

The features list is rendered straight from ava_bridge/features.py's registry,
so a newly registered capability gets its toggle here with no edit to this file
or to the frontend. That is the convention CLAUDE.md enforces via
tests/test_feature_convention.py; do not hand-roll a settings.get_bool here.

Also surfaces `config_error`, so a broken ava.yaml shows as a banner rather than
as settings that silently refuse to save.
"""
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import auth, config, features, settings
from ..version import __version__

router = APIRouter()

# --------------------------------------------------------------------------- #
# System — brand, version, governance mode, feature flags
# --------------------------------------------------------------------------- #
@router.get("/system")
def system():
    voiceprint = any(
        os.path.exists(os.path.join(base, "models", "voiceprint.npy"))
        for base in (settings.AVA_HOME, settings.CODE_ROOT))
    return {
        "brand": settings.brand_name(),
        "version": __version__,
        "code_approval": config.CODE_APPROVAL,
        "learning_enabled": config.LEARNING_ENABLED,
        "learning_interval_h": config.LEARNING_INTERVAL_H,
        # Legacy per-feature booleans (existing consumers) + the registry
        # snapshot the Optional-features panel renders from — one source
        # (features.REGISTRY), so a new capability appears here automatically.
        "voice": features.enabled("voice"),
        "voiceprint": voiceprint,
        "web_search": features.enabled("web_search"),
        "features": features.snapshot(),
        # "" when ava.yaml is fine. When it is not, EVERY setting on this page
        # is silently showing its default and no save will be accepted, so the
        # UI has to say so rather than letting the owner toggle things that
        # cannot persist. connectors/skills surface their load errors the same
        # way — see connectors.load_errors().
        "config_error": settings.load_error(),
        "config_path": str(settings.CONFIG_PATH),
        # "am I running in a container", NOT "is there a docker CLI on PATH".
        # This asked shutil.which("docker"), and deploy/Dockerfile installs only
        # curl and ffmpeg — so every containerized install, which is the primary
        # documented one, reported "Native process" in Setup → System → About.
        # auth.in_container() is the existing answer (it stats /.dockerenv).
        "docker": auth.in_container(),
        "retention_days": settings.data_retention_days(),
        "retention_choices": list(settings.DATA_RETENTION_CHOICES),
        # Editable keys currently shadowed by env vars: a yaml write from the
        # UI "works" but the env value wins again on the next boot. Surfacing
        # the active overrides is the only way the UI can say WHY.
        "env_overrides": {k: v for k, v in {
            "code_approval": settings.env_override("AVA_CODE_APPROVAL"),
            "retention_days": settings.env_override("AVA_DATA_RETENTION_DAYS"),
            "voice": settings.env_override("AVA_VOICE"),
            "voice_threshold": settings.env_override("AVA_PHONE_THRESHOLD"),
            "agent_enabled": settings.env_override("AVA_AGENT_ENABLED"),
            "learning": settings.env_override("AVA_LEARNING"),
        }.items() if v},
    }

@router.post("/system/approval")
def set_approval(mode: str):
    """Set code.approval (all|policy|none) for Ava's self-editing agent.

    Applies LIVE: code_agent reads config.CODE_APPROVAL at call time, so updating
    it here gates the very next code-change request without a restart (the setting
    is also persisted to ava.yaml so it survives one)."""
    mode = (mode or "").strip().lower()
    if mode not in ("all", "policy", "none"):
        return JSONResponse({"ok": False, "error": "mode must be all|policy|none"},
                            status_code=400)
    try:
        settings.save_patch({"code": {"approval": mode}})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    config.CODE_APPROVAL = mode  # take effect immediately, no restart
    return {"ok": True, "restart_required": False,
            # Honest caveat: with the env var set, this live value reverts to
            # the env's on the next boot — the security gate silently flips.
            "env_override": settings.env_override("AVA_CODE_APPROVAL")}

@router.post("/system/retention")
def set_retention(days: int):
    """Set data.retention_days — how long telemetry/history is kept (0 = forever).
    Applies to perf rollups + hardware history on the next restart."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "days must be an integer"},
                            status_code=400)
    if days not in settings.DATA_RETENTION_CHOICES:
        return JSONResponse(
            {"ok": False, "error": f"days must be one of {list(settings.DATA_RETENTION_CHOICES)}"},
            status_code=400)
    try:
        settings.save_patch({"data": {"retention_days": days}})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    return {"ok": True, "restart_required": True,
            "env_override": settings.env_override("AVA_DATA_RETENTION_DAYS")}

