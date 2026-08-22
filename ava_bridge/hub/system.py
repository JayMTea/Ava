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

from .. import auth, features, settings
from ..version import __version__

router = APIRouter()


def _staleness() -> dict:
    """What this process is running vs what is on disk, in one shape.

    Each key answers "is a restart owed, and for what". Every failure degrades to
    a not-known answer rather than a false alarm — a staleness report that cries
    wolf is one an owner learns to dismiss, and then the real one is dismissed too.
    """
    out = {"code": {"stale": False, "known": False},
           "config": {"changed": False, "known": False},
           "router": {"stale": False, "known": False}}
    try:
        from ..version import code_drift
        out["code"] = code_drift()
    except Exception:  # noqa: BLE001
        pass
    try:
        out["config"] = settings.config_drift()
    except Exception:  # noqa: BLE001
        pass
    try:
        # The router holds the backend list it booted with and never re-reads
        # ava.yaml. Comparing its live signature against the config on disk is
        # how "the router is serving a model you have since changed" becomes
        # visible from outside that process.
        from .. import router_app
        from ..router_host import router_boot as _boot
        booted = _boot()
        if booted:
            sig = router_app._backends_sig(router_app.load_backends())
            out["router"] = {"stale": bool(booted.get("backends_sig")
                                           and sig != booted["backends_sig"]),
                             "known": bool(booted.get("backends_sig") and sig),
                             "booted": booted}
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------- #
# System — brand, version, feature flags
# --------------------------------------------------------------------------- #
@router.get("/system")
def system():
    voiceprint = any(
        os.path.exists(os.path.join(base, "models", "voiceprint.npy"))
        for base in (settings.AVA_HOME, settings.CODE_ROOT))
    return {
        "brand": settings.brand_name(),
        "version": __version__,
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
        # Is what is RUNNING still what is on disk? Three frozen facts, each of
        # which has silently outlived its source: the imported code, the parsed
        # ava.yaml, and the router's backend list.
        #
        # A server fact, deliberately. CLAUDE.md: "Drift is a server fact, never
        # client state: it must survive a reload, a second tab, an edit made on
        # disk, and a provision run from the CLI." The restart banner is raised
        # by a mutation RESPONSE today, so an ava.yaml edited in a text editor —
        # the documented way to change several blocks — raises it for nobody.
        "staleness": _staleness(),
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
            "retention_days": settings.env_override("AVA_DATA_RETENTION_DAYS"),
            "voice": settings.env_override("AVA_VOICE"),
            "voice_threshold": settings.env_override("AVA_PHONE_THRESHOLD"),
            "agent_enabled": settings.env_override("AVA_AGENT_ENABLED"),
        }.items() if v},
    }

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

