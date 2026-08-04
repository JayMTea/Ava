"""Access policy for Ava's self-editing — which paths she may change autonomously,
which need the owner's approval, and which are off-limits entirely.

Ava (via Claude) can rewrite most of her own source without asking. But some
paths are sensitive: touching auth, secrets, egress policies or the deploy
scripts could lock the owner out or open a hole, so those changes are STAGED and
dropped into the approval bucket on the Learning page instead of auto-applying.
A few paths (real secrets, biometric voiceprint, git internals) are never
writable by the agent at all.

Classification (first match wins):
    denied   -> refuse the write outright (secrets, models, .git, data/)
    approval -> stage it + create a pending proposal for the owner (critical files)
    auto     -> apply + git-commit immediately (everything else in the repo)

Override the two upper tiers from .env with colon-separated glob lists:
    AVA_CODE_DENY_GLOBS   (added to the built-in deny list)
    AVA_CODE_APPROVAL_GLOBS (added to the built-in approval list)
Globs are matched against the repo-relative POSIX path (e.g. "ava_bridge/auth.py").
"""
import fnmatch
import os

# ---- Never writable by the agent (secrets, biometrics, VCS internals) --------
# These hold credentials, the speaker voiceprint, or git state; a code change
# has no business rewriting them and doing so could brick auth or leak secrets.
_DENY = [
    ".env", ".env.*", "*.env",
    "data/**", "data/*",
    "**/.secret", "**/.internal_token", "**/auth_password",
    "**/setup_claim",           # the first-run claim token (ava_bridge/auth.py)
    "secrets", "secrets/**", "secrets/*",   # router_token, inference_key, env/*
    "models/**",                # biometric voiceprint + TTS/ECAPA weights
    ".git/**",
    ".venv/**",                 # site-packages: writable = arbitrary code on next import
    "run/**",                   # allocator lease/breaker state
    # holds code.approval — writable = self-ungating. The glob covers ava.yaml.bak
    # (written by settings._write_config on every Setup save) and the .tmp it
    # writes through: a denied file whose backup is writable is not denied.
    "ava.yaml*", ".ava.yaml.*",
    "connector_grants.yaml",    # the connector consent ledger = self-approval
    "connector_tools_cache.json",   # declared tool tiers = the JIT-consent surface
    "branding/**",              # the owner's uploaded logo/icon art, not the agent's
    "bin/**", "media/**", "logs/**", "enroll/**",
    "*.onnx", "*.npy", "*.ckpt", "*.ort", "*.pem", "*.key",
]

# ---- Requires the owner's approval (critical control-plane files) -------------
# Changing any of these could affect who can log in, what egress is allowed, how
# the service boots, or the policy engine itself — so they go to the approval
# bucket instead of auto-applying, no matter how confident the agent is.
_APPROVAL = [
    "phone_bridge.py",              # entrypoint + route wiring + auth gate mount
    "ava_bridge/auth.py",          # password / signed-cookie session
    "ava_bridge/config.py",        # secrets loading, tokens, knobs
    "ava_bridge/internal.py",      # X-Ava-Internal-Token gate
    "ava_bridge/access_policy.py", # THIS file — self-referential, must be gated
    "ava_bridge/code_agent.py",    # the autonomous applier itself
    "ava_bridge/coder.py",         # the Claude tool loop
    # _DENY guards ava.yaml, but a denied FILE whose WRITER is auto-editable is
    # not denied — the same lesson as ava.yaml.bak above, one layer up. These
    # three are the whole runtime path that can put `code.approval` on disk:
    # settings.save_patch is the only implementation, hub/system.py is the route
    # that calls it, and the overlay facade mutates _PROJECT_DENY at import.
    # Inert under the shipped `approval: all` (code_agent folds auto into the
    # approval bucket) and live under the documented `approval: policy`.
    "ava_bridge/settings.py",      # save_patch/save_config/_write_config -> ava.yaml
    "ava_bridge/hub/system.py",    # POST /system/approval writes code.approval
    "overlay/ava_bridge/**",       # personal_access.apply() rewrites _PROJECT_DENY
    "agent/policies/**",           # egress / network policies
    # The agent's own system prompt. The template carries the operational
    # mandates that stop the model hallucinating weather and denying it has web
    # access — i.e. the instructions that make its OTHER self-checks meaningful.
    # Auto-editable, an agent could quietly relax the rules it is judged by and
    # commit that itself.
    # render_persona.py is included for the same reason as settings.py above: a
    # gated file whose writer is auto-editable is not gated.
    "agent/persona.txt.tmpl", "agent/render_persona.py",
    "agent/install.sh", "agent/new-tool.sh", "agent/snapshot.sh",
    "run.sh", "run_bridge.sh", "devices.sh",
    "*.service", "*.timer", "*.path",   # systemd units
    "SECURITY.md",
]


def _split_env(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    return [g.strip() for g in raw.split(":") if g.strip()] if raw else []


_DENY += _split_env("AVA_CODE_DENY_GLOBS")
_APPROVAL += _split_env("AVA_CODE_APPROVAL_GLOBS")

# ---- Per-project deny lists for connected repos ------------------------------
# `ava` uses its own _DENY. Connected projects (registered by the optional
# overlay) may attach stricter per-project deny globs; a fresh fork has none.
_PROJECT_DENY = {
    "ava": _DENY,
}
try:
    from overlay.ava_bridge import personal_access as _personal_access
    _personal_access.apply(_PROJECT_DENY, _split_env)
except Exception:  # noqa: BLE001 — no overlay (fork) or a broken overlay
    pass



def _norm(rel: str) -> str:
    return (rel or "").strip().lstrip("/").replace(os.sep, "/")


def _match(rel: str, globs: list[str]) -> bool:
    p = _norm(rel)
    base = p.split("/")[-1]
    for g in globs:
        if fnmatch.fnmatch(p, g) or fnmatch.fnmatch(base, g):
            return True
        # Support "dir/**" also matching the bare "dir/child" one level deep.
        if g.endswith("/**") and p.startswith(g[:-3] + "/"):
            return True
    return False


def classify(rel: str, project: str = "ava") -> str:
    """Return 'denied' | 'approval' | 'auto' for a repo-relative path.

    `project` selects which repo's rules apply. The default "ava" keeps the
    original self-edit tiers. Projects flagged `approval_only` in config.PROJECTS
    route every non-denied path to the approval bucket.
    """
    deny = _PROJECT_DENY.get(project, _DENY)
    if _match(rel, deny):
        return "denied"
    if project != "ava":
        try:
            from . import config as _config
            if _config.PROJECTS.get(project, {}).get("approval_only"):
                return "approval"
        except Exception:  # noqa: BLE001 — be safe: unknown project => approval
            return "approval"
    if _match(rel, _APPROVAL):
        return "approval"
    return "auto"


def classify_edits(edits: list[dict], project: str = "ava") -> dict:
    """Partition staged edits (each a dict with a 'path') by policy tier.

    Returns {"auto": [...], "approval": [...], "denied": [...]} preserving the
    original edit dicts. Used by code_agent to decide apply-now vs route-to-bucket.
    """
    buckets = {"auto": [], "approval": [], "denied": []}
    for e in edits:
        buckets[classify(e.get("path", ""), project)].append(e)
    return buckets
