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

A capability with its own detail panel still belongs here if it reads the
owner's data: the panel explains it, the checkbox turns it off. `features.memory`
is the one such entry — a switch a panel names but nothing renders is worse than
no switch at all.
"""
from . import settings

# key -> {label, sub, default, env, panel, config}. `label`/`sub` feed the Setup
# panel; `default` preserves each flag's historical default; `env` names an
# optional env-var override (see settings.get_bool); `panel: False` keeps a flag
# out of the Optional-features checkboxes (it has its own panel) while still
# routing ALL its reads through this module — tests/test_feature_convention.py
# fails any features.* read that bypasses it. `config` overrides the settings key
# for a capability whose switch predates this registry and is named elsewhere in
# ava.yaml; without it the only way to register such a capability would be to
# rename the owner's config key, which is not a thing a refactor gets to do.
REGISTRY: dict[str, dict] = {
    # EVERY entry carries an `env` key, including the two that manage without one
    # for Ava's own purposes. A control plane can only pin a flag it can set from
    # outside the container, and `settings.get_bool` has nothing to read without
    # one — so an entry with no `env` is a switch that is unpinnable by anything
    # but a config write into the instance. `AVA_WEB_SEARCH` joins the existing
    # `AVA_WEB_*` family in config.py, which tunes the same capability.
    # The agent runtime — the largest optional capability there is, and the one
    # that was outside this registry. Its switch is `agent.enabled`, its panel is
    # Setup → Agent, and its gate lived in runtime/__init__.py emitting prose with
    # no code — so a chat turn that failed because the agent was off or missing
    # said so and offered nowhere to go, while a failed web search got a fix-it
    # link. `panel: False`: it has its own Setup tab, and a checkbox that quietly
    # drops Ava to tool-less chat does not belong in a list of add-ons.
    "agent": {
        "label": "Agent runtime",
        # Deliberately not "a NemoClaw sandbox": this switch governs the agent
        # runtime whichever one is configured, and naming the default here told
        # a `remote` or `openclaw_gw` owner about a runtime they are not running.
        "sub": "tools, memory and skills in the agent's sandbox",
        "default": True,
        "env": "AVA_AGENT_ENABLED",
        "config": "agent.enabled",
        "panel": False,
    },
    # Grouping the connected apps into the owner's own domains, and collecting a
    # daily KPI per domain. OFF by default and deliberately so: it reads app data
    # on a schedule, and a capability that dials the owner's health and money
    # sources should be something they switched on, not something a version bump
    # started doing.
    "domains": {
        "label": "Domains",
        "sub": "group apps into your own domains and track a KPI per domain",
        "default": False,
        "env": "AVA_DOMAINS",
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
    # Editing this install's OWN look: name, colour, logo, icons. On by default,
    # because re-branding the thing you are self-hosting is the premise, not an
    # add-on. This switch is about WHO may edit, never about who has paid;
    # tests/test_no_capability_gate.py fails the build if that ever changes.
    #
    # It gates the WRITE path only. An instance whose brand was set from outside
    # still RENDERS that brand with the switch off; what it loses is the ability
    # to change it. That asymmetry is the whole point of the switch: a control
    # plane that provisions instances can pin AVA_BRANDING=0 from outside the
    # container and hand its members a branded Ava they cannot re-brand, using
    # nothing but the `env` key every entry here already carries.
    "branding": {
        "label": "Branding",
        "sub": "change the name, colour and logo of this install",
        "default": True,
        "env": "AVA_BRANDING",
    },
    # Memory has its own detail panel, but it DOES get a checkbox here: it reads
    # your conversations, and MemoryPanel tells the owner to "enable it in Setup
    # → System" — a control that did not exist, because `panel: False` filtered
    # it out of snapshot(). A switch the UI names and the UI does not render is
    # worse than no switch.
    "memory": {
        "label": "Memory",
        "sub": "distils durable facts from your chats; recalled when relevant",
        "default": True,
        "env": "AVA_MEMORY",
    },
    # Reading the hardware of ANOTHER machine — the one with the GPU — through
    # the Prometheus exporters it already runs, instead of the box the bridge
    # happens to be on. The addresses live under `hardware.exporters` and are
    # edited in Setup → Hardware; this is the switch, and it keeps the one home
    # every switch has. Off by default: the addresses are the owner's to state,
    # and a switch with nothing behind it reads as local either way
    # (ava_bridge/hwexporters.py).
    "remote_hardware": {
        "label": "Remote hardware",
        "sub": "read GPU, memory, CPU and disk from another machine's exporters",
        "default": False,
        "env": "AVA_REMOTE_HARDWARE",
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
    return settings.get_bool(_config_key(key), bool(spec.get("default", False)),
                             env=spec.get("env"))


def _config_key(key: str) -> str:
    """The ava.yaml path behind a registry entry — `features.<key>` unless the
    entry names its own (see REGISTRY's `config`)."""
    return REGISTRY.get(key, {}).get("config") or f"features.{key}"


def preflight(key: str, probe=None) -> tuple[str, str] | None:
    """Gate an execution path on a feature switch. None = go ahead; else
    ``(code, message)``. Checked in this order so a deliberate OFF reads as
    "turned off", never as a misleading outage:

      1. switch off        -> ("<key>_off",  how to turn it on)
      2. probe() truthy    -> ("<key>_down", the probe's actionable error)

    `probe` is an optional zero-arg callable returning None when the backing
    service answers, else an actionable error string naming what to fix.
    """
    label = REGISTRY.get(key, {}).get("label") or key.replace("_", " ")
    if not enabled(key):
        where = ("Setup → System → Optional features"
                 if REGISTRY.get(key, {}).get("panel", True) else "Setup")
        return (f"{key}_off",
                f"{label} is turned off. Enable it under {where} "
                f"({_config_key(key)} in ava.yaml).")
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
    return settings.explicitly_false(_config_key(key), env=spec.get("env"))


def snapshot() -> list[dict]:
    """Optional-features-panel view: every panel feature, in registry order."""
    return [{"key": k, "label": s["label"], "sub": s["sub"],
             "enabled": enabled(k)}
            for k, s in REGISTRY.items() if s.get("panel", True)]
