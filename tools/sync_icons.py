#!/usr/bin/env python3
"""Regenerate the icons DERIVED from Ava's mark, so the tab cannot go stale.

Ava's mark reaches the user through five files. Two are authored, three are not:

    brand/pwa-icon-transparent.svg   master (gitignored, owner-held)
    assets/icons/pwa-512.png         rendered from the master by the design tool
    assets/icons/pwa-192.png         rendered from the master by the design tool

    favicon.ico                      DERIVED from pwa-512 by this file
    favicon.svg                      DERIVED from the master by this file

Nothing regenerates the derived two. Vite copies `public/` verbatim, so updating
pwa-512 alone leaves the browser tab on the previous logo — that happened three
times in one afternoon, twice unnoticed until someone looked at the tab.

WHAT THIS DELIBERATELY DOES NOT DO. It does not touch pwa-192. Measured on this
repo's own history, a natively-rendered 192 differs from a Lanczos downscale of
its own 512 by 3.1 mean absolute error, while a 192 left a full design revision
behind scores 5.6. Thresholding a 2.5-wide gap would fail honest icons and pass
subtle staleness, so this file makes no claim it cannot keep: the two PNGs are
owner-supplied together by one tool, and only their palettes are compared, which
catches a changed palette and nothing finer. Regenerating pwa-192 here would also
be a downgrade — the native render is sharper than any resample of the 512.

WHY THIS IS NOT A `prebuild` STEP. The obvious wiring — package.json, beside
ensure-deps.mjs — is wrong. CI runs `npm ci && npm run build` and byte-compares
frontend/dist (the frontend-dist-drift job). A prebuild hook rewriting tracked
icons would either fire on every CI run and fail that comparison, or no-op
because `brand/` is gitignored and never reaches a runner — a guarantee holding
only on the machine that does not need it. So this is a tool the owner runs, and
tests/test_icon_sync.py is what makes forgetting it fail. That check runs
anywhere, because everything it compares is tracked.

    python tools/sync_icons.py            # regenerate, report what changed
    python tools/sync_icons.py --check    # exit 1 if anything is out of date

Pillow is already a runtime dependency (requirements.txt, for branding and
thumbnails), so this adds nothing to install. Rasterising the SVG master is
deliberately not done here: it needs a headless renderer nobody else has, and
the design tool already emits the PNGs.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "frontend" / "public"
ICONS = PUBLIC / "assets" / "icons"

MASTER = ROOT / "brand" / "pwa-icon-transparent.svg"
SOURCE = ICONS / "pwa-512.png"
PWA_192 = ICONS / "pwa-192.png"
FAVICON_ICO = PUBLIC / "favicon.ico"
FAVICON_SVG = PUBLIC / "favicon.svg"

# The sizes favicon.ico has always carried. Browsers pick per context; 48 is what
# Windows uses for a pinned shortcut, so dropping it costs a visibly blurry icon.
ICO_SIZES = [(16, 16), (32, 32), (48, 48)]

# favicon.ico is written HERE from pwa-512, so a match is exact up to resampler
# noise across Pillow builds — not the wide gap an authored PNG shows. 2/255 is
# far below what an eye resolves and far above that noise.
TOLERANCE = 2.0


def _pixels(img: Image.Image):
    rgba = img.convert("RGBA")
    getter = getattr(rgba, "get_flattened_data", None)
    return getter() if getter else rgba.getdata()


def _mae(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute error per channel between two images of equal size."""
    if a.size != b.size:
        return 255.0
    total = sum(abs(x - y) for pa, pb in zip(_pixels(a), _pixels(b))
                for x, y in zip(pa, pb))
    return total / (a.size[0] * a.size[1] * 4)


def _png_palette(img: Image.Image, top: int = 24) -> set[str]:
    counts = Counter(p[:3] for p in _pixels(img) if p[3] > 200)
    return {f"#{r:02x}{g:02x}{b:02x}" for (r, g, b), _ in counts.most_common(top)}


def _svg_palette(text: str) -> set[str]:
    return {m.group(0).lower() for m in re.finditer(r"#[0-9a-fA-F]{6}", text)}


def _svg_from_master() -> str | None:
    """favicon.svg = this repo's documented header + the master's own drawing.

    The header is carried over rather than regenerated: it records why the tile
    was dropped and what to do if the mark proves too faint at 16px, which is
    knowledge the master does not carry.
    """
    if not MASTER.exists():
        return None
    master = MASTER.read_text(encoding="utf-8")
    inner = master[master.index(">", master.index("<svg")) + 1:master.rindex("</svg>")]
    existing = FAVICON_SVG.read_text(encoding="utf-8") if FAVICON_SVG.exists() else ""
    header = existing[:existing.index("<svg")] if "<svg" in existing else ""
    return (header
            + '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"'
            + ' width="512" height="512" role="img" aria-label="Ava">'
            + inner + "</svg>\n")


def stray_master() -> Path | None:
    """The master parked in public/, where Vite would publish it.

    It keeps landing there because that is where the PNGs it renders to live. The
    consequence is not cosmetic: public/ is copied verbatim into the tracked dist
    and workbox precaches every svg it finds, so the master would ship to every
    client and into the service worker. See the brand/ rule in .gitignore.
    """
    stray = ICONS / "pwa-icon-transparent.svg"
    return stray if stray.exists() else None


def drift() -> list[str]:
    """What is out of date. Empty means every derived icon matches its source."""
    problems: list[str] = []
    if (stray := stray_master()) is not None:
        problems.append(f"{stray.relative_to(ROOT)} belongs in brand/ — public/ is published")
    if not SOURCE.exists():
        problems.append(f"{SOURCE.relative_to(ROOT)} is missing; it is the source for the rest")
        return problems

    src = Image.open(SOURCE).convert("RGBA")
    src_palette = _png_palette(src)

    if not FAVICON_ICO.exists():
        problems.append("frontend/public/favicon.ico is missing")
    else:
        for size in ICO_SIZES:
            frame = Image.open(FAVICON_ICO)
            frame.size = size
            err = _mae(frame, src.resize(size, Image.LANCZOS))
            if err > TOLERANCE:
                problems.append(
                    f"favicon.ico {size[0]}px is stale against pwa-512.png "
                    f"(mean error {err:.1f}) — the tab is on a different logo")
                break

    # favicon.svg's source is gitignored, so it cannot be diffed against the
    # master on a fresh clone. Its palette can still be compared with the PNG's,
    # and that is the failure that actually occurred: the tab kept a flat #007ACC
    # tile for two rounds after the mark became a gradient.
    if not FAVICON_SVG.exists():
        problems.append("frontend/public/favicon.svg is missing")
    elif (declared := _svg_palette(FAVICON_SVG.read_text(encoding="utf-8"))):
        if not declared & src_palette:
            problems.append("favicon.svg shares no colour with pwa-512.png — "
                            "the tab is on a different logo from the app icon")

    # Weak by construction, and labelled as such: it catches a changed palette,
    # not a changed shape. See the module docstring for why nothing stronger is
    # claimed about an authored PNG.
    if PWA_192.exists():
        if not _png_palette(Image.open(PWA_192)) & src_palette:
            problems.append("pwa-192.png shares no colour with pwa-512.png — "
                            "one of them was not re-exported")
    else:
        problems.append("frontend/public/assets/icons/pwa-192.png is missing")
    return problems


def sync() -> list[str]:
    """Regenerate every derived icon. Returns what was written."""
    written: list[str] = []
    if (stray := stray_master()) is not None:
        MASTER.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stray), str(MASTER))
        written.append(f"moved {stray.relative_to(ROOT)} -> {MASTER.relative_to(ROOT)}")

    src = Image.open(SOURCE).convert("RGBA")
    src.save(FAVICON_ICO, format="ICO", sizes=ICO_SIZES)
    written.append(str(FAVICON_ICO.relative_to(ROOT)))

    if (svg := _svg_from_master()) is not None:
        FAVICON_SVG.write_text(svg, encoding="utf-8")
        written.append(str(FAVICON_SVG.relative_to(ROOT)))
    else:
        written.append(f"skipped favicon.svg — no {MASTER.relative_to(ROOT)} here")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="report drift and exit 1 instead of writing anything")
    args = ap.parse_args()

    if args.check:
        problems = drift()
        for p in problems:
            print(f"[icons] {p}", file=sys.stderr)
        if problems:
            print("[icons] run: python tools/sync_icons.py", file=sys.stderr)
            return 1
        print("[icons] favicon.ico and favicon.svg match pwa-512.png")
        return 0

    for line in sync():
        print(f"[icons] {line}")
    print("[icons] now run `npm run build` in frontend/ and commit dist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
