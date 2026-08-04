"""The tab icon must be the same logo as the app icon.

favicon.ico and favicon.svg are DERIVED from Ava's mark and nothing regenerates
them: Vite copies `public/` verbatim, so a design revision that updates
assets/icons/pwa-512.png alone leaves the browser tab on the previous logo. That
is not hypothetical — it happened three times in one afternoon, twice unnoticed
until someone looked at a tab, because every test was green each time.

This is the guard, in the tests/test_diagram_sync.py style: a static comparison
that runs anywhere and fails with the command that fixes it. It compares only
tracked files, so it works on a fresh clone where brand/ (gitignored) is absent.

What it deliberately does NOT assert is in tools/sync_icons.py's docstring: a
natively-rendered pwa-192 differs from a downscale of its own 512 by about as
much as a fully stale one does, so nothing finer than a palette check is claimed
about the authored PNGs.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    from PIL import Image  # noqa: F401
    _HAVE_PILLOW = True
except Exception:
    _HAVE_PILLOW = False

sys.path.insert(0, str(ROOT / "tools"))


@unittest.skipUnless(_HAVE_PILLOW, "needs Pillow (declared in requirements.txt)")
class IconSyncTests(unittest.TestCase):

    def test_the_derived_icons_match_the_mark_they_come_from(self):
        import sync_icons

        problems = sync_icons.drift()
        self.assertEqual(problems, [], "\n  " + "\n  ".join(problems) +
                         "\n\nRun `python tools/sync_icons.py`, then rebuild the "
                         "frontend and commit dist.")

    def test_the_master_is_not_sitting_in_a_published_directory(self):
        """public/ is copied verbatim into the tracked dist AND precached by
        workbox, so a master parked beside the PNGs it renders ships to every
        client. It lands there repeatedly because that is where its output lives.
        """
        import sync_icons

        stray = sync_icons.stray_master()
        self.assertIsNone(stray, f"{stray} must live in brand/ — see .gitignore")

    def test_the_checker_actually_fails_on_a_stale_icon(self):
        """A guard nobody has seen fail is a guard nobody knows works.

        Rather than trusting that `drift()` would notice, this feeds it the real
        thing: favicon.ico regenerated from a DIFFERENT mark, in a throwaway tree.
        """
        import shutil
        import tempfile

        import sync_icons
        from PIL import Image

        with tempfile.TemporaryDirectory(prefix="ava-icon-sync-") as tmp:
            tmp_root = Path(tmp)
            icons = tmp_root / "frontend" / "public" / "assets" / "icons"
            icons.mkdir(parents=True)
            shutil.copy(sync_icons.SOURCE, icons / "pwa-512.png")
            shutil.copy(sync_icons.PWA_192, icons / "pwa-192.png")
            shutil.copy(sync_icons.FAVICON_SVG, tmp_root / "frontend" / "public" / "favicon.svg")
            # a favicon from a mark that is not this one: same geometry, inverted
            src = Image.open(sync_icons.SOURCE).convert("RGBA")
            wrong = Image.eval(src, lambda v: 255 - v)
            wrong.save(tmp_root / "frontend" / "public" / "favicon.ico",
                       format="ICO", sizes=sync_icons.ICO_SIZES)

            for name, value in (("ROOT", tmp_root),
                                ("PUBLIC", tmp_root / "frontend" / "public"),
                                ("ICONS", icons),
                                ("SOURCE", icons / "pwa-512.png"),
                                ("PWA_192", icons / "pwa-192.png"),
                                ("FAVICON_ICO", tmp_root / "frontend" / "public" / "favicon.ico"),
                                ("FAVICON_SVG", tmp_root / "frontend" / "public" / "favicon.svg"),
                                ("MASTER", tmp_root / "brand" / "x.svg")):
                self.addCleanup(setattr, sync_icons, name, getattr(sync_icons, name))
                setattr(sync_icons, name, value)

            problems = sync_icons.drift()

        self.assertTrue(any("favicon.ico" in p for p in problems),
                        f"a favicon from a different mark was not caught: {problems}")

    def test_the_tool_reports_drift_through_its_exit_code(self):
        """CI and a human both read the exit code, so it has to mean something."""
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "sync_icons.py"), "--check"],
                           capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
