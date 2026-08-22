"""config.example.yaml is the only documentation of what Ava can be configured to
do, so a key the code reads and the template omits is a knob nobody can discover.

`features.memory` was exactly that: the kill switch for the capability the README
leads with ("It remembers — and you hold the eraser") was in `features.REGISTRY`,
honoured at runtime, and absent from the template — so the only way to turn it
off was to already know it existed. In the other direction `features.voiceprint`
was documented and is not a registry key at all.

SCOPE, stated deliberately. A full bidirectional scan of every dotted key was
attempted and abandoned: ~29 keys the code "does not read" it reads through
helpers (`features.enabled()`, whole-dict lookups like `alloc.models`), and ~41
it "does not document" are documented as commented-out YAML blocks. A guard that
needs a 30-entry allowlist to stay green is a guard nobody maintains, and its
failures teach people to add allowlist entries rather than fix drift.

So this checks the three things that are exactly checkable, and says so:
  * the feature registry and the template agree, both ways,
  * every `[AVA_*]` annotation names an env var some module actually reads,
  * the setting whose default is a security decision has not drifted back.

Style matches tests/test_diagram_sync.py: a `git ls-files` scan, no bridge, no
network.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-tmplsync-test-"))

from ava_bridge import features

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config.example.yaml"

# Documented in the template but deliberately not a features.REGISTRY entry.
_NOT_REGISTRY_FEATURES = {
    # A personal biometric add-on, gated by enrollment rather than by the
    # optional-features registry (there is no Setup checkbox for it).
    "voiceprint",
}

_ENV_ANNOTATION = re.compile(r"\[(AVA_[A-Z0-9_]+)\]")
_ENV_LITERAL = re.compile(r"""["'](AVA_[A-Z0-9_]+)["']""")


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _features_block() -> dict[str, str]:
    """The `features:` block's keys, including commented-out ones."""
    body, seen = TEMPLATE.read_text(encoding="utf-8"), {}
    inside = False
    for line in body.splitlines():
        if re.match(r"^features:\s*$", line):
            inside = True
            continue
        if inside:
            if line and not line[0].isspace():
                break
            m = re.match(r"^\s+#?\s*([a-z_]+):\s*(\S+)?", line)
            if m and m.group(2) is not None:
                seen[m.group(1)] = m.group(2)
    return seen


class FeatureRegistrySyncTests(unittest.TestCase):
    def test_every_registered_feature_is_documented(self):
        documented = set(_features_block())
        # A capability whose switch predates the registry keeps its own ava.yaml
        # key (REGISTRY `config`) and is documented beside it — see
        # test_feature_convention.py, which asserts that documentation exists.
        # The rule is that an owner can find the switch, not that every switch
        # was moved under `features:`.
        own_key = {k for k, spec in features.REGISTRY.items() if spec.get("config")}
        missing = sorted(set(features.REGISTRY) - documented - own_key)
        self.assertFalse(missing, (
            "these capabilities are honoured at runtime but appear nowhere in "
            "config.example.yaml, so the only way to turn one off is to already "
            "know it exists — which is how `features.memory`, the kill switch "
            "for the feature the README leads with, went undocumented: "
            f"{missing}"))

    def test_every_documented_feature_is_real(self):
        documented = set(_features_block()) - _NOT_REGISTRY_FEATURES
        unknown = sorted(documented - set(features.REGISTRY))
        self.assertFalse(unknown, (
            "config.example.yaml documents these under `features:` but they are "
            "not in features.REGISTRY, so setting one does nothing. Either "
            "register the capability or drop it from the template (add it to "
            f"_NOT_REGISTRY_FEATURES here only if it is deliberately outside the "
            f"registry, with a reason): {unknown}"))

    def test_the_documented_defaults_match_the_registry(self):
        block = _features_block()
        offenders = []
        for key, spec in features.REGISTRY.items():
            if key not in block:
                continue
            want = "true" if spec.get("default") else "false"
            got = block[key].strip().lower().rstrip(",")
            if got != want:
                offenders.append(f"{key}: template says {got}, registry default is {want}")
        self.assertFalse(offenders, (
            "the template's stated defaults disagree with features.REGISTRY, so "
            "the documentation describes an install the user will not get: "
            f"{offenders}"))


class EnvAnnotationTests(unittest.TestCase):
    def test_every_annotated_env_var_is_actually_read(self):
        annotated = set(_ENV_ANNOTATION.findall(TEMPLATE.read_text(encoding="utf-8")))
        self.assertTrue(annotated, "no [AVA_*] annotations found — did the format change?")
        used = set()
        for rel in _tracked("*.py"):
            if rel.startswith(("tests/", "qa/")):
                continue
            used |= set(_ENV_LITERAL.findall(_read(rel)))
        stale = sorted(annotated - used)
        self.assertFalse(stale, (
            "config.example.yaml annotates these as env overrides but no module "
            "reads them, so a forker setting one gets silence: " + str(stale)))


class SecurityDefaultLockTests(unittest.TestCase):
    """This default is a security decision, not a preference. Locking it here
    means a future edit has to argue with a test rather than slip through.

    There were two. The other was `code.approval: all` — the gate on Ava editing
    her own source — and it went with the capability it gated."""

    def test_the_template_binds_loopback(self):
        body = TEMPLATE.read_text(encoding="utf-8")
        m = re.search(r"^\s*host:\s*(\S+)", body, re.MULTILINE)
        self.assertIsNotNone(m, "no server.host in the template")
        self.assertIn(m.group(1), ("127.0.0.1", "localhost", "::1"), (
            "config.example.yaml must ship a loopback bind. `ava setup` copies "
            "this file's posture, and until the owner sets a password `/setup` "
            "is necessarily reachable by anyone who can reach the port."))


if __name__ == "__main__":
    unittest.main()
