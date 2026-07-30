"""Optional-feature registry — the ONE place a user-facing capability is named.

Adding a capability here is all it takes for the rest of the system to pick it
up automatically:

  * the Setup → System → Optional features panel renders a checkbox for it
    (hub_api /system exposes ``snapshot()``),
  * the setup save endpoint accepts its toggle (``REGISTRY`` is the whitelist),
  * ``preflight(key, probe)`` guards its execution path with regular,
    machine-readable error codes — ``<key>_off`` when the switch is off,
    ``<key>_down`` when the switch is on but the backing service won't answer —
    which the chat UI turns into a "here's where to fix it" link
    (frontend/src/lib/fixes.ts derives the link from the code PATTERN, so no
    frontend change is needed for a new feature),
  * and the plain-text message is self-contained ("Enable it under Setup →
    System → Optional features"), so agent tools that just relay the error
    (e.g. web_search.mjs) already tell the user what to do.

Feature flags that are NOT user-facing capabilities (features.learning,
features.memory) stay out of this registry — they have their own panels.
"""
from . import settings

# key -> {label, sub, default, env, panel}. `label`/`sub` feed the Setup panel;
# `default` preserves each flag's historical default; `env` names an optional
# env-var override (see settings.get_bool); `panel: False` keeps a flag out of
# the Optional-features checkboxes (it has its own panel) while still routing
# ALL its reads through this module — tests/test_feature_convention.py fails
# any features.* read that bypasses it.
REGISTRY: dict[str, dict] = {
    # EVERY entry carries an `env` key, including the two that manage without one
    # for Ava's own purposes. A control plane can only pin a flag it can set from
    # outside the container, and `settings.get_bool` has nothing to read without
    # one — so an entry with no `env` is a switch that is unpinnable by anything
    # but a config write into the instance. `AVA_WEB_SEARCH` joins the existing
    # `AVA_WEB_*` family in config.py, which tunes the same capability.
    "image": {
        "label": "Image / video generation",
        "sub": "via the the GPU service connector",
        "default": True,
        "env": "AVA_IMAGE",
    },
    "web_search": {
        "label": "Web search",
        "sub": "self-hosted SearXNG + guarded fetch",
        "default": False,
        "env": "AVA_WEB_SEARCH",
    },
    "voice": {
        "label": "Voice",
        "sub": "push-to-talk (needs requirements-voice.txt)",
        "default": False,
        "env": "AVA_VOICE",
    },
    # Both have their own detail panels, but they DO get a checkbox here: each
    # one reads your conversations, and LearningView/MemoryPanel both tell the
    # owner to "enable it in Setup → System" — a control that did not exist,
    # because `panel: False` filtered them out of snapshot(). A switch the UI
    # names and the UI does not render is worse than no switch.
    "learning": {
        "label": "Learning",
        "sub": "periodic local-first self-analysis of your activity",
        "default": True,
        "env": "AVA_LEARNING",
    },
    "memory": {
        "label": "Memory",
        "sub": "distils durable facts from your chats; recalled when relevant",
        "default": True,
        "env": "AVA_MEMORY",
    },
    # Separate from `learning` on purpose — see learning._cloud_fallback_enabled.
    # Learning prompts embed verbatim excerpts from every conversation, so
    # sending them off-box is its own decision, defaulting to no.
    "learning_cloud_fallback": {
        "label": "Cloud fallback for learning",
        "sub": "if the local model can't finish a cycle, send the prompt "
               "(which quotes your chats) to Anthropic",
        "default": False,
        "env": "AVA_LEARNING_CLOUD_FALLBACK",
    },
}


def enabled(key: str) -> bool:
    """Live state of one feature switch (yaml, honoring its env override).

    Unregistered keys (e.g. a connector manifest's `feature:` name that isn't
    in the registry yet) default ON: no UI switch exists for them, so nothing
    may silently read as "off by choice"."""
    spec = REGISTRY.get(key)
    if spec is None:
        return settings.get_bool(f"features.{key}", True)
    return settings.get_bool(f"features.{key}", bool(spec.get("default", False)),
                             env=spec.get("env"))


def preflight(key: str, probe=None) -> tuple[str, str] | None:
    """Gate an execution path on a feature switch. None = go ahead; else
    ``(code, message)``. Checked in this order so a deliberate OFF reads as
    "turned off", never as a misleading outage:

      1. switch off        -> ("<key>_off",  how to turn it on)
      2. probe() truthy    -> ("<key>_down", the probe's actionable error)

    `probe` is an optional zero-arg callable returning None when the backing
    service answers, else an error string (e.g. gpu_jobs.gpusvc_reachable).
    """
    label = REGISTRY.get(key, {}).get("label") or key.replace("_", " ")
    if not enabled(key):
        return (f"{key}_off",
                f"{label} is turned off. Enable it under Setup → System → "
                f"Optional features (features.{key} in ava.yaml).")
    if probe is not None:
        err = probe()
        if err:
            return (f"{key}_down", err)
    return None


def explicitly_off(key: str) -> bool:
    """True only when the switch was DELIBERATELY turned off (yaml false or
    env 0) — unset stays permissive. For paths that work purely because their
    deps are installed (e.g. /api/talk on installs that never wrote
    features.voice), where an unset flag must not read as a refusal."""
    spec = REGISTRY.get(key, {})
    return settings.explicitly_false(f"features.{key}", env=spec.get("env"))


def snapshot() -> list[dict]:
    """Optional-features-panel view: every panel feature, in registry order."""
    return [{"key": k, "label": s["label"], "sub": s["sub"],
             "enabled": enabled(k)}
            for k, s in REGISTRY.items() if s.get("panel", True)]
