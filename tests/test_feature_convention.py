"""Convention guard: ALL features.* reads go through ava_bridge/features.py.

This is the enforcement half of the guided-fix convention (docs/CONNECTOR_SDK.md
§6, CLAUDE.md): every user-facing capability is one registry entry, gated with
features.preflight()/enabled()/explicitly_off(), which is what gives it its
Setup checkbox, its regular <key>_off/<key>_down error codes, and the chat's
fix-it links — automatically. A hand-rolled settings.get_bool("features.…")
bypasses all of that, so this test fails it with instructions. Style follows
test_diagram_sync.py: a static scan over tracked sources that runs anywhere,
including CI.
"""
import pathlib
import re

import yaml

from ava_bridge import features
from gitfiles import tracked_paths as _tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The one module allowed to touch features.* settings directly.
ALLOWED = {"ava_bridge/features.py"}

# A read of a features.* key through the settings layer, e.g.:
#   settings.get_bool("features.memory", ...)
#   settings.explicitly_false('features.voice', ...)
#   settings.get("features.web_search")
_DIRECT_READ = re.compile(
    r"(?:get_bool|get_int|explicitly_false|\bget)\(\s*['\"]features\.")

_FIX = (
    "\n\nRoute it through the capability registry instead "
    "(ava_bridge/features.py):\n"
    "  - features.enabled(key)        boolean state\n"
    "  - features.preflight(key, probe=...)  gate an execution path; returns\n"
    "    (\"<key>_off\"/\"<key>_down\", message) which the chat renders as a\n"
    "    guided fix-it link automatically\n"
    "  - features.explicitly_off(key) deliberate-off-only checks\n"
    "New capability? Add ONE entry to features.REGISTRY — checkbox, save\n"
    "whitelist, error codes and fix links all follow from it.\n"
    "See docs/CONNECTOR_SDK.md §6 and CLAUDE.md."
)




def test_no_feature_reads_bypass_the_registry() -> None:
    offenders = []
    for path in _tracked("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED or rel.startswith("tests/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _DIRECT_READ.search(line):
                offenders.append(f"  {rel}:{i}: {line.strip()}")
    assert not offenders, (
        "Direct features.* read(s) bypass ava_bridge/features.py:\n"
        + "\n".join(offenders) + _FIX)


def test_manifest_feature_flags_are_registered() -> None:
    """A connector's `service.feature` must name a registered capability —
    otherwise its dashboard off-paint and its gates can silently disagree."""
    unknown = []
    for path in _tracked("connectors/*/connector.yaml"):
        try:
            m = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue  # malformed manifests are surfaced by connectors.load_errors
        feat = ((m.get("service") or {}).get("feature"))
        if feat and feat not in features.REGISTRY:
            unknown.append(f"  {path.relative_to(ROOT)}: feature: {feat}")
    assert not unknown, (
        "Connector manifest(s) reference unregistered features:\n"
        + "\n".join(unknown)
        + "\n\nAdd the key to features.REGISTRY (ava_bridge/features.py)"
          " so the switch, error codes, and fix-it links exist." + _FIX)


def test_every_feature_is_pinnable_from_outside_the_process() -> None:
    """Each REGISTRY entry must name an `env` override.

    `settings.get_bool` reads the env var named here; with no `env` key there is
    nothing to read, so the only way to change the switch is a config write
    *inside* the instance. That makes the flag unpinnable by anything outside it
    — a control plane, a compose profile, a systemd unit, or a `docker run -e`.

    This was not hypothetical: `web_search` shipped without one, so a control
    plane's feature ceiling could express only some of the capability switches
    and silently ignore the rest — including the one flag that reaches the
    network.
    """
    missing = sorted(k for k, spec in features.REGISTRY.items() if not spec.get("env"))
    assert not missing, (
        f"features.REGISTRY entries with no `env` override: {missing}. Add "
        '`"env": "AVA_<KEY>"` to each — without it the switch can only be changed '
        "by writing config inside the instance, so nothing outside the process "
        "(a control plane, compose, docker -e) can pin it." + _FIX)


def test_feature_env_names_do_not_collide() -> None:
    """Two features sharing an env var means setting one silently sets the other."""
    seen: dict[str, str] = {}
    for key, spec in features.REGISTRY.items():
        env = spec.get("env")
        if not env:
            continue
        assert env not in seen, (
            f"{key} and {seen[env]} both use {env} — pinning one would silently "
            "pin the other." + _FIX)
        seen[env] = key


def test_config_example_documents_exactly_the_registered_features() -> None:
    """`config.example.yaml`'s features block must match REGISTRY, both ways.

    A key here that REGISTRY does not have is a switch a user can set and nothing
    reads — this file documented `features.voiceprint: false` for exactly that
    reason, so an owner could turn off speaker verification in their config and
    still be verified on every voice turn. A REGISTRY key missing here is an
    undiscoverable capability. Both directions are asserted, because only one of
    them is the bug that already happened.
    """
    import yaml as _yaml

    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    doc = _yaml.safe_load(text) or {}
    documented = set((doc.get("features") or {}).keys())
    registered = set(features.REGISTRY)
    assert not (documented - registered), (
        f"config.example.yaml documents unregistered feature(s) "
        f"{sorted(documented - registered)} — a switch an owner can set that "
        "nothing reads. Register it in features.REGISTRY or delete the line."
        + _FIX)
    assert not (registered - documented), (
        f"feature(s) {sorted(registered - documented)} are in REGISTRY but not in "
        "config.example.yaml, so nobody reading the reference config knows they "
        "exist. Add a line with its `[AVA_*]` env marker in the comment." + _FIX)


def test_the_env_override_is_named_beside_each_documented_feature() -> None:
    """The `[AVA_*]` marker is how an owner discovers the override at all."""
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    block = text.split("features:", 1)[1].split("\n\n", 1)[0]
    missing = sorted(spec["env"] for spec in features.REGISTRY.values()
                     if spec.get("env") and f"[{spec['env']}]" not in block)
    assert not missing, (
        f"env override(s) {missing} are not named in config.example.yaml's "
        "features block, so the only way to find them is to read features.py. "
        "Add `# [AVA_KEY]` to the relevant comment." + _FIX)
