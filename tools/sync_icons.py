#!/usr/bin/env python3
"""Regenerate the icons DERIVED from Ava's mark, so the tab cannot go stale.

Ava's mark reaches the user through seven files. Two are authored, five are not:

    brand/pwa-icon-transparent.svg   master (gitignored, owner-held)
    assets/icons/pwa-512.png         rendered from the master by the design tool
    assets/icons/pwa-192.png         rendered from the master by the design tool

    favicon.ico                      DERIVED from pwa-512 by this file
    favicon.svg                      DERIVED from the master by this file
    assets/icons/pwa-maskable-512.png  DERIVED from pwa-512 + the master's stops
    assets/icons/apple-touch-icon.png  DERIVED the same way, at 180

Nothing regenerates the derived ones. Vite copies `public/` verbatim, so updating
pwa-512 alone leaves the browser tab on the previous logo — that happened three
times in one afternoon, twice unnoticed until someone looked at the tab.

THE TWO FULL-BLEED ICONS WERE MISSING FROM THAT LIST, AND WENT STALE EXACTLY AS
PREDICTED. `drift()` below records that the tab "kept a flat #007ACC tile for two
rounds after the mark became a gradient". The fix guarded favicon.svg and stopped
there — so pwa-maskable-512 and apple-touch-icon sat on that same #007ACC, with a
different logo inside it, for every release since. It surfaced when an installed
PWA showed one mark and the docs site showed another: a browser tab reads
favicon.*, but an INSTALLED app takes its window and taskbar icon from the
manifest, where `purpose: maskable` wins. Nothing here was checking that file.

They are full-bleed by requirement, not by taste: a maskable icon is masked into a
circle or squircle by the OS, so a transparent one gets whatever background the
launcher picks. That is why they cannot simply be pwa-512 — they are the mark in
white over the master's own gradient, at the scale the previous pair established.

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
# (filename, size, mark width, mark height) — a NAME, not a path, so it resolves
# against `ICONS` when it is used. tests/test_icon_sync.py rebinds these module
# constants to a throwaway tree to prove the checker fails; a Path baked in here
# would ignore that and point back at the real repo.
#
# The fractions are MEASURED from the icons these replace, not chosen: the old
# maskable put its mark in a 53% x 69% box and apple-touch in 47% x 61%, both
# centred. Keeping them means the tile reads at the same weight in a launcher as
# it always has, and only the mark and the palette change.
#
# 53% also keeps the mark inside the maskable safe zone in the direction that
# matters. The spec reserves a circle of 80% diameter; a 53%-wide mark survives
# the squircle every shipping launcher actually applies, which is far more
# forgiving than the worst-case circle.
FULL_BLEED = [
    ("pwa-maskable-512.png", 512, 0.53, 0.69),
    ("apple-touch-icon.png", 180, 0.47, 0.61),
]

# How far a full-bleed icon's background may sit from a colour the mark actually
# uses before it counts as a different palette. Measured on the failure this
# caught: the stale #007ACC backdrop is 22/255 from the nearest current stop
# (#0064E0), a freshly generated one is 0. 12 sits between them with room either
# side, and is well under what an eye reads as the same blue.
BG_TOLERANCE = 12

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


def _gradient_stops() -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """(top, bottom) RGB of the master's own gradient, or None without a master.

    Read rather than hardcoded so a palette revision reaches the full-bleed icons
    by regenerating, not by someone remembering a hex here. `y1 > y2` means the
    ramp runs bottom-to-top, so offset 0 is the BOTTOM stop — getting that
    backwards inverts the tile and nothing else notices.
    """
    if not MASTER.exists():
        return None
    text = MASTER.read_text(encoding="utf-8")
    grad = re.search(r"<linearGradient[^>]*>(.*?)</linearGradient>", text, re.S)
    if not grad:
        return None
    stops = re.findall(r'stop-color="(#[0-9a-fA-F]{6})"', grad.group(1))
    if len(stops) < 2:
        return None
    first, second = (_rgb(stops[0]), _rgb(stops[1]))
    y1 = re.search(r'y1="([-\d.]+)"', grad.group(0))
    y2 = re.search(r'y2="([-\d.]+)"', grad.group(0))
    bottom_first = bool(y1 and y2 and float(y1.group(1)) > float(y2.group(1)))
    return (second, first) if bottom_first else (first, second)


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _full_bleed(size: int, frac_w: float, frac_h: float,
                stops: tuple[tuple[int, int, int], tuple[int, int, int]]) -> Image.Image:
    """The mark in white over the master's gradient, centred, filling the canvas.

    The shape comes from pwa-512's ALPHA rather than from a second drawing, so
    these can never be a different logo from the tab — which is the whole failure
    being fixed. Rasterising the SVG would need a renderer this repo deliberately
    does not depend on (see the module docstring).
    """
    top, bottom = stops
    src = Image.open(SOURCE).convert("RGBA")
    mark = src.split()[3]
    mark = mark.crop(mark.getbbox())

    scale = min(size * frac_w / mark.width, size * frac_h / mark.height)
    mark = mark.resize((max(1, round(mark.width * scale)),
                        max(1, round(mark.height * scale))), Image.LANCZOS)

    ramp = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        ramp.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * t)
                                    for i in range(3)))
    canvas = ramp.resize((size, size), Image.BICUBIC).convert("RGBA")
    canvas.paste(Image.new("RGBA", mark.size, (255, 255, 255, 255)),
                 ((size - mark.width) // 2, (size - mark.height) // 2), mark)
    return canvas.convert("RGB")


def _corners(img: Image.Image) -> list[tuple[int, int, int]]:
    """The four corner pixels — unambiguously backdrop on a full-bleed icon.

    NOT the most common colour, which is what this asked first and got wrong. The
    backdrop is a gradient, so each of its 512 rows holds about 512 pixels while
    the white mark is one colour across ~40,000 — "dominant" returns WHITE, which
    is 128 from the nearest blue, and the check failed every icon it was given.
    It only runs without a master, so it ran on CI and nowhere else.

    The corners have no such ambiguity: the mark is centred and at most 69% tall,
    so nothing but backdrop reaches them, and on a gradient they sample both ends
    of the ramp for free.
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    return [rgb.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]


def _colour_gap(colour: tuple[int, int, int], palette: set[str]) -> int:
    """Distance from `colour` to the nearest palette entry, worst channel."""
    if not palette:
        return 255
    return min(max(abs(a - b) for a, b in zip(colour, _rgb(hexed)))
               for hexed in palette)


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

    # The full-bleed pair. Checked two ways for the same reason favicon.svg is
    # checked weakly: the master is gitignored, so a fresh clone cannot reproduce
    # them exactly. With a master present the comparison is exact; without one it
    # falls back to asking whether the backdrop is still a colour the mark uses,
    # which is precisely the drift that went unnoticed for two rounds.
    stops = _gradient_stops()
    for name, size, fw, fh in FULL_BLEED:
        path = ICONS / name
        rel = path.relative_to(ROOT)
        if not path.exists():
            problems.append(f"{rel} is missing")
            continue
        actual = Image.open(path).convert("RGBA")
        if actual.size != (size, size):
            problems.append(f"{rel} is {actual.size[0]}px, expected {size}px")
            continue
        if stops is not None:
            err = _mae(actual, _full_bleed(size, fw, fh, stops).convert("RGBA"))
            if err > TOLERANCE:
                problems.append(
                    f"{rel} is stale against pwa-512.png (mean error {err:.1f}) — "
                    "the installed app is on a different logo from the tab")
            continue
        gap = max(_colour_gap(c, src_palette) for c in _corners(actual))
        if gap > BG_TOLERANCE:
            problems.append(
                f"{rel}'s backdrop is {gap} off the nearest colour in pwa-512.png "
                "— the installed app is on a different palette from the tab")
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

    # Same rule as favicon.svg: the gradient stops live in the master, so without
    # one these are left alone rather than written from a guessed palette.
    if (stops := _gradient_stops()) is not None:
        for name, size, fw, fh in FULL_BLEED:
            path = ICONS / name
            _full_bleed(size, fw, fh, stops).save(path, format="PNG")
            written.append(str(path.relative_to(ROOT)))
    else:
        written.append(f"skipped the full-bleed icons — no {MASTER.relative_to(ROOT)} here")
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
