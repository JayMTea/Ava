"""A connector manifest is a file the OWNER writes. Editing it must not eat it.

Three defects lived in the two Hub routes that change one field of a manifest —
the enable/disable toggle and the appearance picker:

  * They round-tripped the whole file through `yaml.safe_dump`, so ticking a
    checkbox deleted every comment and blank line the owner had put in it. The
    loader's own rule says the opposite: "the owner asked for that YAML; a loader
    that 'repairs' it loses whatever they were in the middle of writing."
  * `yaml.YAMLError` escaped an `except OSError`, so the ONE connector whose YAML
    had a typo answered with an unhandled 500 — from the screen whose job is to
    repair it. You could not disable the connector that was broken.
  * The write was truncating, not tmp+rename, unlike the fourteen other writers
    in the codebase that get this right.

House style: stdlib unittest, real temp dirs, no bridge, no network.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

import yaml

from ava_bridge import connectors, settings

COMMENTED = """\
# My notes about this app, which I would like to keep.
id: myapp
label: My App          # what shows in the rail
enabled: true

ui:
  # picked deliberately, do not auto-change
  icon: grid
  color: "#abcdef"
  embed: iframe
actions:
  - id: read_notes     # safe, never asks
    path: /notes
"""


class PatchTests(unittest.TestCase):
    """`patch_manifest_text` — surgical, verified, and willing to give up."""

    def test_a_top_level_toggle_touches_exactly_one_line(self):
        out = connectors.patch_manifest_text(COMMENTED, ("enabled",), False)
        self.assertIsNotNone(out)
        before, after = COMMENTED.splitlines(), out.splitlines()
        self.assertEqual(len(before), len(after))
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(differing), 1, "more than the target line moved")
        self.assertEqual(after[differing[0]].strip(), "enabled: false")

    def test_every_comment_survives(self):
        out = connectors.patch_manifest_text(COMMENTED, ("ui", "icon"), "bolt")
        for comment in ("# My notes about this app",
                        "# what shows in the rail",
                        "# picked deliberately",
                        "# safe, never asks"):
            self.assertIn(comment, out, f"lost {comment!r}")

    def test_a_trailing_comment_on_the_edited_line_is_kept(self):
        src = "id: x\nenabled: true  # I turned this on\n"
        out = connectors.patch_manifest_text(src, ("enabled",), False)
        self.assertEqual(out, "id: x\nenabled: false  # I turned this on\n")

    def test_a_hash_inside_a_quoted_value_is_not_a_comment(self):
        """The most common appearance edit there is. A naive `[^#]*` split reads
        the `#` in `"#abcdef"` as the start of a comment and corrupts the line —
        and every connector colour is a hex value."""
        src = 'id: x\nui:\n  color: "#abcdef"   # brand blue\n  icon: grid\n'
        out = connectors.patch_manifest_text(src, ("ui", "color"), "#123456")
        self.assertEqual(yaml.safe_load(out)["ui"]["color"], "#123456")
        self.assertIn("# brand blue", out)
        self.assertEqual(yaml.safe_load(out)["ui"]["icon"], "grid")

    def test_removing_a_key_removes_the_line(self):
        out = connectors.patch_manifest_text(COMMENTED, ("ui", "icon"),
                                             connectors.REMOVE)
        self.assertNotIn("icon:", out)
        self.assertIn("embed: iframe", out)
        self.assertIn("# picked deliberately", out)

    def test_removing_an_absent_key_is_a_no_op(self):
        self.assertEqual(
            connectors.patch_manifest_text(COMMENTED, ("ui", "shape"),
                                           connectors.REMOVE),
            COMMENTED)

    def test_a_missing_key_is_inserted_into_its_block(self):
        out = connectors.patch_manifest_text("id: x\nui:\n  color: 2\n",
                                             ("ui", "icon"), "bolt")
        self.assertEqual(yaml.safe_load(out)["ui"],
                         {"color": 2, "icon": "bolt"})

    def test_it_declines_rather_than_guessing(self):
        for name, src, keys in (
            ("no parent block", "id: x\n", ("ui", "icon")),
            ("duplicated key", "id: x\nenabled: true\nenabled: false\n", ("enabled",)),
            ("value is a block", "id: x\nenabled:\n  - a\n", ("enabled",)),
            ("file does not parse", "id: x\n  bad: [\n", ("enabled",)),
            ("too deep", "a:\n  b:\n    c: 1\n", ("a", "b", "c")),
        ):
            with self.subTest(name=name):
                self.assertIsNone(
                    connectors.patch_manifest_text(src, keys, False),
                    "guessed instead of falling back to a safe re-dump")

    def test_the_result_always_parses_to_the_intended_value(self):
        """The round-trip check is what makes a text edit safe to ship."""
        for keys, value in ((("enabled",), False),
                            (("ui", "icon"), "bolt"),
                            (("ui", "color"), None)):
            with self.subTest(keys=keys):
                out = connectors.patch_manifest_text(COMMENTED, keys, value)
                got = yaml.safe_load(out)
                target = got if len(keys) == 1 else got[keys[0]]
                self.assertEqual(target[keys[-1]], value)


class RouteTests(unittest.TestCase):
    """The two routes, against a real file."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ava-manifest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, "connectors", "myapp")
        os.makedirs(self.dir)
        self.path = os.path.join(self.dir, "connector.yaml")
        self._write(COMMENTED)

        from ava_bridge.hub import connectors as hub_connectors
        self.hub = hub_connectors
        for p in (
            mock.patch.object(connectors, "USER_DIR",
                                       os.path.join(self.tmp, "connectors")),
            mock.patch.object(connectors, "BUILTIN_DIR",
                                       os.path.join(self.tmp, "connectors")),
            mock.patch.object(hub_connectors.perf_mgmt, "refresh_sources"),
        ):
            p.start()
            self.addCleanup(p.stop)
        connectors.load(force=True)

    def _write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self) -> str:
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_disabling_keeps_the_owners_comments(self):
        r = self.hub.set_connector_enabled("myapp", {"enabled": False})
        self.assertTrue(r["ok"], r)
        text = self._read()
        self.assertIn("# My notes about this app", text)
        self.assertIn("# safe, never asks", text)
        self.assertFalse(yaml.safe_load(text)["enabled"])

    def test_restyling_keeps_them_too(self):
        r = self.hub.set_connector_appearance("myapp", {"icon": "bolt"})
        self.assertTrue(r["ok"], r)
        text = self._read()
        self.assertIn("# picked deliberately", text)
        self.assertEqual(yaml.safe_load(text)["ui"]["icon"], "bolt")

    def test_clearing_removes_the_key_not_nulls_it(self):
        self.hub.set_connector_appearance("myapp", {"icon": None})
        self.assertNotIn("icon", yaml.safe_load(self._read())["ui"])

    def test_a_manifest_that_does_not_parse_is_a_409_not_a_500(self):
        """The connector you cannot disable used to be the broken one."""
        self._write("id: myapp\nui: [unclosed\n")
        r = self.hub.set_connector_enabled("myapp", {"enabled": False})
        self.assertEqual(r.status_code, 409)
        body = r.body.decode()
        self.assertIn("connector.yaml", body)
        self.assertIn("does not parse", body)

    def test_the_broken_manifest_is_left_exactly_as_it_was(self):
        broken = "id: myapp\nui: [unclosed\n"
        self._write(broken)
        self.hub.set_connector_enabled("myapp", {"enabled": False})
        self.hub.set_connector_appearance("myapp", {"icon": "bolt"})
        self.assertEqual(self._read(), broken,
                         "a failed edit rewrote the file it could not read")

    def test_the_write_is_atomic(self):
        """A crash mid-write must leave the previous manifest, not half of one."""
        real = settings.atomic_write

        def boom(path, body, mode=None):
            raise OSError("no space left on device")

        with mock.patch.object(settings, "atomic_write", boom):
            r = self.hub.set_connector_enabled("myapp", {"enabled": False})
        self.assertEqual(r.status_code, 500)
        self.assertEqual(self._read(), COMMENTED)
        self.assertIs(settings.atomic_write, real)


class AtomicWriteTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ava-atomic-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_it_replaces_the_whole_file(self):
        p = os.path.join(self.tmp, "f.txt")
        settings.atomic_write(p, "one")
        settings.atomic_write(p, "two")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "two")

    def test_a_failed_write_leaves_the_old_file_and_no_litter(self):
        p = os.path.join(self.tmp, "f.txt")
        settings.atomic_write(p, "original")
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                settings.atomic_write(p, "replacement")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "original")
        self.assertEqual([n for n in os.listdir(self.tmp) if n != "f.txt"], [],
                         "a temp file was left behind")

    def test_the_mode_lands_with_the_content(self):
        p = os.path.join(self.tmp, "secret.txt")
        settings.atomic_write(p, "x", mode=0o600)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_it_creates_missing_parents(self):
        p = os.path.join(self.tmp, "a", "b", "f.txt")
        settings.atomic_write(p, "x")
        self.assertTrue(os.path.isfile(p))


if __name__ == "__main__":
    unittest.main()
