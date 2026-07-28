"""`ava connector new` must scaffold something that WORKS when you follow it.

It emitted its own inline template rather than copying
connectors/_template/connector.yaml, and the two had drifted in ways that only
bite after you accept the file's own invitation to uncomment something:

  * the inline sample action was `- { id: NAME_do, description: "..." }` with no
    `path:` — and both `connectors.tool_files()` and `render_egress_policy()`
    require one. So `ava connector tools` and `ava connector policies` printed
    NOTHING, with no error, for a manifest the scaffolder had just written.
  * its perf path pointed inside `$AVA_HOME/connectors/<id>/`, which the
    reference template explicitly warns against, because removing the connector
    then takes its Vitals history with it.

Fixed by deletion: the command copies the reference template, so a second one
cannot exist to drift. These tests pin both halves — that it is a copy, and that
following it end to end produces real tools and a real policy.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-scaffold-test-"))

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "connectors" / "_template" / "connector.yaml"


def _ava(home: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "ava_cli.py"), *args],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ, "AVA_HOME": home})


def _uncomment_actions(path: pathlib.Path) -> None:
    """Uncomment the FIRST (list-form) `actions:` block, as the file invites.

    Only the first: the template documents a LIST form and a DICT form, and
    uncommenting both leaves two `actions:` keys, of which YAML silently keeps
    the last. That is a quirk of this helper, not of the template.
    """
    out, inside, done = [], False, False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# actions:") and not inside and not done:
            inside = True
            out.append("actions:")
            continue
        if inside:
            if line.startswith("#   ") or line.strip() == "#":
                out.append(line[2:] if line.startswith("# ") else line)
                continue
            inside, done = False, True
        out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


class ScaffoldIsACopyTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ava-scaf-")
        r = _ava(self.home, "connector", "new", "demoapp")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.path = pathlib.Path(self.home) / "connectors" / "demoapp" / "connector.yaml"

    def test_it_writes_a_manifest(self):
        self.assertTrue(self.path.is_file())

    def test_it_is_the_reference_template_modulo_the_id(self):
        """Byte-equality after substitution is what makes divergence impossible."""
        want = TEMPLATE.read_text(encoding="utf-8")
        want = want.replace("id: myapp", "id: demoapp")
        want = want.replace("label: My App", "label: demoapp").replace("myapp", "demoapp")
        self.assertEqual(self.path.read_text(encoding="utf-8"), want)

    def test_the_perf_path_stays_outside_the_connector_directory(self):
        """Inside it, removing the connector destroys its Vitals history."""
        body = self.path.read_text(encoding="utf-8")
        m = re.search(r"path:\s*\"\$\{AVA_[A-Z_]+\}[^\"]*performance\.jsonl\"", body)
        self.assertIsNotNone(m, "the perf example vanished from the template")
        self.assertNotIn("connectors/demoapp/performance.jsonl", m.group(0))

    def test_it_refuses_to_overwrite(self):
        r = _ava(self.home, "connector", "new", "demoapp")
        self.assertNotEqual(r.returncode, 0)


class FollowingTheTemplateProducesOutputTests(unittest.TestCase):
    """The actual bug: uncomment the sample action, and get nothing."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ava-scaf2-")
        _ava(self.home, "connector", "new", "demoapp")
        self.path = pathlib.Path(self.home) / "connectors" / "demoapp" / "connector.yaml"
        _uncomment_actions(self.path)

    def test_the_sample_action_declares_a_path(self):
        """Without one, tool_files() and render_egress_policy() both yield
        nothing — silently."""
        import yaml
        m = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        actions = m.get("actions")
        self.assertTrue(actions, "uncommenting `actions:` produced no actions")
        first = actions[0] if isinstance(actions, list) else None
        self.assertIsInstance(first, dict)
        self.assertTrue(first.get("path"), f"sample action has no `path:`: {first}")

    def test_connector_tools_generates_something(self):
        r = _ava(self.home, "connector", "tools", "demoapp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip(), (
            "`ava connector tools` printed nothing for a manifest the scaffolder "
            "wrote and the user uncommented as instructed"))

    def test_connector_policies_generates_something(self):
        r = _ava(self.home, "connector", "policies", "demoapp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/internal/connector/demoapp", r.stdout)

    def test_the_scaffolded_manifest_passes_the_validator(self):
        import yaml

        from ava_bridge import connectors
        m = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        errors: list = []
        connectors._validate(m, str(self.path), errors)
        hard = [e for e in errors if e.get("severity") != "warn"]
        self.assertFalse(hard, f"the scaffolded manifest is invalid: {hard}")


if __name__ == "__main__":
    unittest.main()
