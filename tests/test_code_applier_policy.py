"""The decision layer that gates an LLM writing to your source tree.

`code_agent.py` (571 LOC), `coder.py` (533) and `access_policy.py` had no direct
test anywhere in tests/ or qa/ — the single highest-consequence untested path in
the repo, since what it decides is whether a model's edit lands on disk
automatically, waits for a human, or is refused outright.

These are pure functions over paths and edit lists: no network, no sandbox, no
Claude key. There was never a technical reason they were untested.

Deliberately covers the *tiers*, not the applier's IO: an over-permissive
classification is the failure that matters, and it is fully expressible here.
"""
import os
import unittest
from unittest import mock

from ava_bridge import access_policy, code_agent


class ClassifyTiers(unittest.TestCase):
    def test_secrets_and_state_are_denied(self):
        for rel in (".env", ".env.local", "prod.env",
                    "data/chats.json", "data/.internal_token",
                    "ava_bridge/.secret", "data/auth_password",
                    "models/voiceprint.npy", ".git/config",
                    "logs/audit.jsonl", "media/out/x.png",
                    "certs/server.pem", "certs/server.key"):
            with self.subTest(rel=rel):
                self.assertEqual(access_policy.classify(rel), "denied", rel)

    def test_control_plane_needs_approval(self):
        for rel in ("phone_bridge.py", "ava_bridge/auth.py", "ava_bridge/config.py",
                    "ava_bridge/internal.py", "ava_bridge/access_policy.py",
                    "ava_bridge/code_agent.py", "ava_bridge/coder.py",
                    "agent/policies/ava-web.yaml", "agent/install.sh",
                    "run.sh", "ava-bridge.service", "SECURITY.md"):
            with self.subTest(rel=rel):
                self.assertEqual(access_policy.classify(rel), "approval", rel)

    def test_the_policy_file_gates_itself(self):
        """Self-referential on purpose: an edit that widens the policy must not
        be able to land automatically and thereby widen everything after it."""
        self.assertEqual(
            access_policy.classify("ava_bridge/access_policy.py"), "approval")

    def test_ordinary_code_is_auto(self):
        for rel in ("ava_bridge/turns.py", "ava_bridge/hub_api.py",
                    "frontend/src/App.tsx", "docs/CHOOSE_A_MODEL.md",
                    "tests/test_router.py"):
            with self.subTest(rel=rel):
                self.assertEqual(access_policy.classify(rel), "auto", rel)

    def test_deny_beats_approval(self):
        """A path matching both lists must resolve to the stricter tier."""
        with mock.patch.dict(access_policy._PROJECT_DENY, {}, clear=False):
            self.assertEqual(access_policy.classify("data/anything.py"), "denied")

    def test_unknown_project_falls_back_to_avas_own_tiers(self):
        """An unrecognised project name resolves to Ava's own repo
        (code_agent._project_cfg) and therefore to Ava's tiers — coherent, since
        the edit lands in Ava's tree. The deny list still applies."""
        self.assertEqual(
            access_policy.classify("some/ordinary/file.py", project="not-a-project"),
            "auto")
        self.assertEqual(
            access_policy.classify("data/chats.json", project="not-a-project"),
            "denied")

    def test_a_broken_project_config_fails_closed(self):
        """If config.PROJECTS cannot be read, the tier degrades to approval
        rather than auto — the one place `classify` deliberately fails closed."""
        from ava_bridge import config as config_mod

        class Unreadable:
            def get(self, *_a, **_kw):
                raise RuntimeError("config unavailable")

        with mock.patch.object(config_mod, "PROJECTS", Unreadable()):
            self.assertEqual(
                access_policy.classify("ordinary.py", project="other"), "approval")


class ClassifyEditsPartition(unittest.TestCase):
    def test_edits_are_partitioned_and_preserved(self):
        edits = [
            {"path": "ava_bridge/turns.py", "content": "a"},
            {"path": "phone_bridge.py", "content": "b"},
            {"path": "data/chats.json", "content": "c"},
            {"path": "docs/README.md", "content": "d"},
        ]
        out = access_policy.classify_edits(edits)
        self.assertEqual([e["path"] for e in out["auto"]],
                         ["ava_bridge/turns.py", "docs/README.md"])
        self.assertEqual([e["path"] for e in out["approval"]], ["phone_bridge.py"])
        self.assertEqual([e["path"] for e in out["denied"]], ["data/chats.json"])
        # the original dicts come back, not copies stripped of their payload
        self.assertEqual(out["auto"][0]["content"], "a")

    def test_empty_input_yields_three_empty_buckets(self):
        self.assertEqual(access_policy.classify_edits([]),
                         {"auto": [], "approval": [], "denied": []})

    def test_a_missing_path_key_does_not_raise(self):
        out = access_policy.classify_edits([{"content": "x"}])
        self.assertEqual(sum(len(v) for v in out.values()), 1)


class RestartDecision(unittest.TestCase):
    """`_needs_bridge_restart` decides whether applying an edit bounces the
    running service. Wrong in one direction it restarts on a docs typo; wrong in
    the other, a live code change silently does not take effect."""

    def test_runtime_code_triggers_a_restart(self):
        for path in ("phone_bridge.py", "ava_bridge/turns.py",
                     "agent/mcp_server_content/x.py", "agent/install.sh",
                     "run.sh"):
            with self.subTest(path=path):
                self.assertTrue(
                    code_agent._needs_bridge_restart([{"path": path}]), path)

    def test_non_runtime_files_do_not(self):
        for path in ("docs/CHOOSE_A_MODEL.md", "frontend/src/App.tsx",
                     "config.example.yaml", "README.md"):
            with self.subTest(path=path):
                self.assertFalse(
                    code_agent._needs_bridge_restart([{"path": path}]), path)

    def test_one_runtime_edit_in_a_batch_is_enough(self):
        self.assertTrue(code_agent._needs_bridge_restart(
            [{"path": "README.md"}, {"path": "ava_bridge/turns.py"}]))

    def test_empty_batch_does_not_restart(self):
        self.assertFalse(code_agent._needs_bridge_restart([]))


class ProjectResolution(unittest.TestCase):
    def test_unknown_project_falls_back_to_avas_own_repo(self):
        cfg = code_agent._project_cfg("definitely-not-configured")
        self.assertIsInstance(cfg, dict)

    def test_ava_project_root_is_the_repo(self):
        cfg = code_agent._project_cfg("ava")
        root = cfg.get("root") or ""
        if root:
            self.assertTrue(os.path.isabs(root), "project roots must be absolute")


class EnvExtensionOfThePolicy(unittest.TestCase):
    def test_operator_can_widen_the_deny_list(self):
        """AVA_CODE_DENY_GLOBS is read at import, so this asserts the parser
        rather than re-importing the module. Separator is ':' (PATH-style), not
        a comma — a glob may legitimately contain a comma."""
        self.assertEqual(access_policy._split_env("NOPE_NOT_SET"), [])
        with mock.patch.dict(os.environ, {"X_GLOBS": "a/**: b/*  :: c.py"}):
            self.assertEqual(access_policy._split_env("X_GLOBS"),
                             ["a/**", "b/*", "c.py"])


class StagedEditsStayInsideTheRoot(unittest.TestCase):
    """Path escapes are the other way an applier goes wrong: a traversal in a
    model-supplied path writing outside the project entirely.

    Containment is enforced by coder._safe() at the file-IO layer, NOT by
    access_policy.classify() — classify is a pure tier lookup over a relative
    path string and says "auto" for "../x.py" because tiering is not its job.
    Asserting containment on the wrong layer would have looked like a passing
    security test while testing nothing.
    """

    def test_safe_refuses_paths_that_escape_the_repo(self):
        from ava_bridge import coder
        for rel in ("../outside.py", "../../etc/passwd",
                    "ava_bridge/../../escape.py", "/etc/shadow"):
            with self.subTest(rel=rel):
                try:
                    resolved = coder._safe(rel)
                except ValueError:
                    continue  # refused, as required
                root = os.path.realpath(coder.ROOT)
                self.assertTrue(
                    resolved == root or resolved.startswith(root + os.sep),
                    f"{rel!r} resolved outside the repo: {resolved}")

    def test_safe_accepts_ordinary_paths(self):
        from ava_bridge import coder
        p = coder._safe("ava_bridge/turns.py")
        self.assertTrue(p.endswith(os.path.join("ava_bridge", "turns.py")))


if __name__ == "__main__":
    unittest.main()
