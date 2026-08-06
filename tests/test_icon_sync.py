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
            # Copied before ICONS is rebound below, so these come from the repo.
            for name, _size, _fw, _fh in sync_icons.PWA_PNGS:
                shutil.copy(sync_icons.ICONS / name, icons / name)
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

    def test_the_checker_works_on_a_fresh_clone_where_the_master_is_absent(self):
        """The path CI takes, and the one a dev box never runs.

        brand/ is gitignored. While the two PWA icons carried a tile, its gradient
        could only be read from the master, so `drift()` had a CI-only fallback
        that sniffed the backdrop colour instead of comparing — and the first
        version of that fallback shipped broken, failing every icon, on CI alone.
        Untiling them removed the need for it: every input is tracked now, so this
        asserts the ordinary comparison runs and passes with no master at all.
        """
        import sync_icons

        self.addCleanup(setattr, sync_icons, "MASTER", sync_icons.MASTER)
        sync_icons.MASTER = Path("/nonexistent/brand/pwa-icon-transparent.svg")

        problems = sync_icons.drift()
        self.assertEqual(problems, [], "\n  " + "\n  ".join(problems) +
                         "\n\nThis is the check a fresh clone and CI run.")

    def test_a_re_tiled_icon_is_caught_on_a_fresh_clone_too(self):
        """The regression that would undo this, checked where it would land.

        A tile is how the installed app came to show a different mark from the
        docs tab: the icons stayed the right size and shape and grew a backdrop.
        Fed a flat #007ACC one — the real stale colour — in a throwaway tree with
        no master, which is what CI has.
        """
        import shutil
        import tempfile

        import sync_icons
        from PIL import Image

        with tempfile.TemporaryDirectory(prefix="ava-icon-fresh-") as tmp:
            tmp_root = Path(tmp)
            public = tmp_root / "frontend" / "public"
            icons = public / "assets" / "icons"
            icons.mkdir(parents=True)
            shutil.copy(sync_icons.SOURCE, icons / "pwa-512.png")
            shutil.copy(sync_icons.PWA_192, icons / "pwa-192.png")
            shutil.copy(sync_icons.FAVICON_SVG, public / "favicon.svg")
            shutil.copy(sync_icons.FAVICON_ICO, public / "favicon.ico")
            for name, size, _fw, _fh in sync_icons.PWA_PNGS:
                tiled = Image.open(sync_icons.ICONS / name).convert("RGBA")
                backdrop = Image.new("RGBA", (size, size), (0, 122, 204, 255))
                backdrop.alpha_composite(tiled)
                backdrop.save(icons / name)

            for name, value in (("ROOT", tmp_root), ("PUBLIC", public),
                                ("ICONS", icons),
                                ("SOURCE", icons / "pwa-512.png"),
                                ("PWA_192", icons / "pwa-192.png"),
                                ("FAVICON_ICO", public / "favicon.ico"),
                                ("FAVICON_SVG", public / "favicon.svg"),
                                ("MASTER", tmp_root / "brand" / "absent.svg")):
                self.addCleanup(setattr, sync_icons, name, getattr(sync_icons, name))
                setattr(sync_icons, name, value)

            problems = sync_icons.drift()

        for name, _size, _fw, _fh in sync_icons.PWA_PNGS:
            self.assertTrue(any(name in p for p in problems),
                            f"a re-tiled {name} was not caught without a master: {problems}")

    def test_the_checker_catches_a_stale_INSTALLED_app_icon(self):
        """The gap that let a real one ship.

        A browser tab reads favicon.*, but an installed PWA takes its window and
        taskbar icon from the manifest, where `purpose: maskable` wins — so
        pwa-maskable-512.png can be a different logo from the tab and every check
        stays green. It was, for months: a flat #007ACC tile holding the previous
        letterform, while the tab carried the current gradient network mark. It
        was still a different logo after that tile was regenerated rather than
        removed, because a white mark on blue is not the mark the tab shows.

        Fed the real thing, in a throwaway tree: the mark inverted, so the file is
        the right size and shape and the wrong logo.
        """
        import shutil
        import tempfile

        import sync_icons
        from PIL import Image

        with tempfile.TemporaryDirectory(prefix="ava-icon-mask-") as tmp:
            tmp_root = Path(tmp)
            public = tmp_root / "frontend" / "public"
            icons = public / "assets" / "icons"
            icons.mkdir(parents=True)
            shutil.copy(sync_icons.SOURCE, icons / "pwa-512.png")
            shutil.copy(sync_icons.PWA_192, icons / "pwa-192.png")
            shutil.copy(sync_icons.FAVICON_SVG, public / "favicon.svg")
            shutil.copy(sync_icons.FAVICON_ICO, public / "favicon.ico")
            for name, size, _fw, _fh in sync_icons.PWA_PNGS:
                good = Image.open(sync_icons.ICONS / name).convert("RGB")
                Image.eval(good, lambda v: 255 - v).save(icons / name)
                self.assertEqual(good.size, (size, size))

            for name, value in (("ROOT", tmp_root), ("PUBLIC", public),
                                ("ICONS", icons),
                                ("SOURCE", icons / "pwa-512.png"),
                                ("PWA_192", icons / "pwa-192.png"),
                                ("FAVICON_ICO", public / "favicon.ico"),
                                ("FAVICON_SVG", public / "favicon.svg")):
                self.addCleanup(setattr, sync_icons, name, getattr(sync_icons, name))
                setattr(sync_icons, name, value)

            problems = sync_icons.drift()

        for name, _size, _fw, _fh in sync_icons.PWA_PNGS:
            self.assertTrue(any(name in p for p in problems),
                            f"a stale {name} was not caught: {problems}")

    def test_the_tool_reports_drift_through_its_exit_code(self):
        """CI and a human both read the exit code, so it has to mean something."""
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "sync_icons.py"), "--check"],
                           capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
