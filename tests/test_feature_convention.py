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
#   settings.get_bool("features.image", ...)
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
