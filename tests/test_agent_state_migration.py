"""Generated agent material moves off the code root, once, without losing work.

`agent/policies/generated/` and `agent/mcp_server_connectors/apps/` hold files
RENDERED from connector manifests. Both are gitignored — the tell that they are
runtime state — and both used to be joined onto the code root. That is correct on
bare metal, where AVA_HOME *is* the code root, and wrong on the install shape most
people run: the code root is an image layer, so every rebuild threw the generated
tools and egress policies away while keeping the manifests that produced them, and
the connector came back listed and silently un-deployed.

`settings.agent_state_dir()` owns the location now. Anyone who connected an app
before that has files at the old path, so the move has to happen for them — and it
has to be the kind of move that can be interrupted, re-run, and run on every boot
forever without doing damage.

House style: stdlib unittest, no bridge, no network.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ava_bridge import settings


class MigrationTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ava-agent-state-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.code = self.tmp / "code"
        self.state = self.tmp / "home" / "agent"
        (self.code / "agent").mkdir(parents=True)

    def _legacy(self, *parts: str, body: str = "x") -> Path:
        p = self.code / "agent" / Path(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def _migrate(self) -> list[str]:
        with mock.patch.object(settings, "CODE_ROOT", self.code), \
             mock.patch.object(settings, "agent_state_dir",
                               return_value=str(self.state)):
            return settings.migrate_agent_state(write=True)

    def test_a_dry_run_moves_nothing(self):
        """The default. It reports what it WOULD adopt and touches no file."""
        self._legacy("policies", "generated", "acme.yaml")
        with mock.patch.object(settings, "CODE_ROOT", self.code), \
             mock.patch.object(settings, "agent_state_dir",
                               return_value=str(self.state)):
            planned = settings.migrate_agent_state()
        self.assertEqual(planned, [os.path.join("policies", "generated", "acme.yaml")])
        self.assertTrue(
            (self.code / "agent" / "policies" / "generated" / "acme.yaml").exists(),
            "a dry run moved the file anyway")
        self.assertFalse(self.state.exists(), "a dry run created the destination")

    def test_ensure_dirs_never_adopts_anything(self):
        """The regression that bit during development, and the reason this is a
        command rather than a boot step.

        `ensure_dirs()` runs at `config` import, and `agent/install.sh` shells
        `python3 -c 'from ava_bridge import config'` to resolve the bridge port.
        With the migration wired into `ensure_dirs`, ANY process importing the
        bridge under a different AVA_HOME — a test harness, a `deploy/slot.sh`
        staging slot, an `ava` run pointed at a second home — moved the primary
        install's generated tools and policies into that other home. A secondary
        AVA_HOME is a reader of the code root, never an heir to it.
        """
        legacy = self._legacy("policies", "generated", "acme.yaml", body="KEEP\n")
        with mock.patch.object(settings, "CODE_ROOT", self.code), \
             mock.patch.object(settings, "agent_state_dir",
                               return_value=str(self.state)), \
             mock.patch.object(settings, "data_dir", return_value=str(self.tmp / "d")), \
             mock.patch.object(settings, "logs_dir", return_value=str(self.tmp / "l")), \
             mock.patch.object(settings, "upload_dir", return_value=str(self.tmp / "u")), \
             mock.patch.object(settings, "secrets_dir", return_value=str(self.tmp / "s")), \
             mock.patch.object(settings, "brand_dir", return_value=str(self.tmp / "b")), \
             mock.patch.object(settings, "home", side_effect=lambda *p: str(self.tmp.joinpath(*p))):
            settings.ensure_dirs()
        self.assertEqual(legacy.read_text(), "KEEP\n",
                         "ensure_dirs() moved a file out of the code root")

    def test_ensure_dirs_does_not_call_the_migration_at_all(self):
        """Belt and braces on the above: the wiring, not just the outcome."""
        import inspect
        src = inspect.getsource(settings.ensure_dirs)
        body = "\n".join(ln for ln in src.splitlines()
                          if not ln.lstrip().startswith("#"))
        self.assertNotIn("migrate_agent_state", body,
                         "ensure_dirs() calls the migration again")

    def test_it_moves_both_generated_trees(self):
        self._legacy("policies", "generated", "acme.yaml", body="preset: {}\n")
        self._legacy("mcp_server_connectors", "apps", "acme", "acme_call.mjs",
                     body="// tool\n")

        moved = self._migrate()

        self.assertEqual(
            (self.state / "policies" / "generated" / "acme.yaml").read_text(),
            "preset: {}\n")
        self.assertEqual(
            (self.state / "mcp_server_connectors" / "apps" / "acme"
             / "acme_call.mjs").read_text(), "// tool\n")
        self.assertEqual(len(moved), 2, moved)
        # And it left nothing behind at the old path to be found by a stale reader.
        self.assertFalse(
            (self.code / "agent" / "policies" / "generated" / "acme.yaml").exists())

    def test_it_is_a_no_op_when_the_two_roots_are_the_same_directory(self):
        """The plain-checkout case, which is most people. Nothing to move, and
        emphatically nothing to delete."""
        self._legacy("policies", "generated", "acme.yaml")
        with mock.patch.object(settings, "CODE_ROOT", self.code), \
             mock.patch.object(settings, "agent_state_dir",
                               return_value=str(self.code / "agent")):
            self.assertEqual(settings.migrate_agent_state(), [])
        self.assertTrue(
            (self.code / "agent" / "policies" / "generated" / "acme.yaml").exists())

    def test_a_second_run_does_nothing(self):
        self._legacy("policies", "generated", "acme.yaml")
        self.assertEqual(len(self._migrate()), 1)
        self.assertEqual(self._migrate(), [],
                         "the migration is not idempotent, so it would churn on "
                         "every single boot")

    def test_the_new_root_wins_and_is_never_clobbered(self):
        """Re-running after a partial move must finish it, not overwrite work the
        owner has done since."""
        (self.state / "policies" / "generated").mkdir(parents=True)
        (self.state / "policies" / "generated" / "acme.yaml").write_text(
            "NEW\n", encoding="utf-8")
        self._legacy("policies", "generated", "acme.yaml", body="OLD\n")
        self._legacy("policies", "generated", "other.yaml", body="OTHER\n")

        moved = self._migrate()

        self.assertEqual(
            (self.state / "policies" / "generated" / "acme.yaml").read_text(), "NEW\n")
        self.assertEqual(
            (self.state / "policies" / "generated" / "other.yaml").read_text(), "OTHER\n")
        self.assertEqual(moved, [os.path.join("policies", "generated", "other.yaml")])

    def test_nothing_to_move_is_not_an_error(self):
        self.assertEqual(self._migrate(), [])

    def test_it_never_raises(self):
        """It runs from `ensure_dirs()`, which runs at import. A migration that
        cannot complete must not stop the bridge booting — the worst case is the
        behaviour that was already shipping."""
        self._legacy("policies", "generated", "acme.yaml")
        with mock.patch.object(settings, "CODE_ROOT", self.code), \
             mock.patch.object(settings, "agent_state_dir",
                               return_value=str(self.state)), \
             mock.patch.object(settings._shutil, "move",
                               side_effect=OSError("read-only volume")):
            self.assertEqual(settings.migrate_agent_state(write=True), [])

    def test_the_resolvers_agree_with_the_trees_it_moves(self):
        """`GENERATED_AGENT_TREES` and the two path helpers are one fact; a new
        generated tree added to one and not the other would migrate to nowhere."""
        state = settings.agent_state_dir()
        derived = {
            os.path.relpath(settings.generated_policy_dir(), state),
            os.path.relpath(settings.connector_tools_dir(), state),
        }
        declared = {os.path.join(*parts)
                    for parts in settings.GENERATED_AGENT_TREES}
        self.assertEqual(derived, declared)


if __name__ == "__main__":
    unittest.main()
