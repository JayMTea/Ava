#!/usr/bin/env python3
"""Regenerate the icons DERIVED from Ava's mark, so the tab cannot go stale.

Ava's mark reaches the user through seven files. Two are authored, five are not:

    brand/pwa-icon-transparent.svg   master (gitignored, owner-held)
    assets/icons/pwa-512.png         rendered from the master by the design tool
    assets/icons/pwa-192.png         rendered from the master by the design tool

    favicon.ico                      DERIVED from pwa-512 by this file
    favicon.svg                      DERIVED from the master by this file
    assets/icons/pwa-maskable-512.png  DERIVED from pwa-512 by this file, inset
    assets/icons/apple-touch-icon.png  DERIVED from pwa-512 by this file, at 180

Nothing regenerates the derived ones. Vite copies `public/` verbatim, so updating
pwa-512 alone leaves the browser tab on the previous logo — that happened three
times in one afternoon, twice unnoticed until someone looked at the tab.

THE TWO PWA ICONS WERE MISSING FROM THAT LIST, AND WENT STALE EXACTLY AS
PREDICTED. `drift()` below records that the tab "kept a flat #007ACC tile for two
rounds after the mark became a gradient". The fix guarded favicon.svg and stopped
there — so pwa-maskable-512 and apple-touch-icon sat on that same #007ACC, with a
different logo inside it, for every release since. It surfaced when an installed
PWA showed one mark and the docs site showed another: a browser tab reads
favicon.*, but an INSTALLED app takes its window and taskbar icon from the
manifest, where `purpose: maskable` wins. Nothing here was checking that file.

THE FIRST FIX KEPT THE TILE, AND THAT WAS THE WRONG HALF. It regenerated the two
from the current mark but drew it in WHITE over the master's gradient, on the
argument that a maskable icon is full-bleed by contract and iOS composites
transparency onto black. Both statements are true and neither was worth the
result: white-on-blue at tab size is a different mark from blue-on-nothing, so
the same install still showed two logos side by side — the docs tab and the
installed app — which is the complaint the whole file exists to prevent.

So they are now the same drawing as everything else: pwa-512's own pixels,
colour and alpha, over nothing. What that costs is real and accepted. iOS
composites the transparency onto black, which the mark survives because every
colour in it is a bright blue. A launcher applying a maskable mask supplies its
own background, usually white, which is the same thing the docs site shows. The
53% inset below stays exactly as it was: that is the maskable safe zone, and it
is about where the mark sits, not what is behind it.

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
# `None` fractions mean "keep pwa-512's own framing" — a plain resize, so the
# file is the tab icon at another size and cannot drift from it by construction.
# That is what apple-touch wants now that it has no tile to sit inside: iOS
# rounds the corners, and the corners of this mark are empty.
#
# The maskable one keeps its 53% x 69% inset, which was measured from the icon it
# replaced. That number is about the SAFE ZONE, not the tile: the spec reserves a
# circle of 80% diameter, and a 53%-wide mark survives the squircle every shipping
# launcher actually applies. pwa-512 fills 80% x 96% of its canvas, so resizing it
# here instead would put the mark's top and bottom under the mask.
PWA_PNGS = [
    ("pwa-maskable-512.png", 512, 0.53, 0.69),
    ("apple-touch-icon.png", 180, None, None),
]

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


def _pwa_png(size: int, frac_w: float | None, frac_h: float | None) -> Image.Image:
    """pwa-512's own pixels at `size`, optionally inset, over nothing.

    Colour AND alpha come from pwa-512, not from a second drawing and not from a
    palette read out of the master, so these cannot be a different logo — or a
    different blue — from the tab. It also means every input is tracked, which is
    what lets `drift()` compare exactly on a fresh clone where brand/ is absent.

    With no fractions this is a plain resize. With them the mark is cropped to its
    own bounding box and centred in a `frac_w` x `frac_h` box, which is how the
    maskable one stays inside the safe zone.
    """
    src = Image.open(SOURCE).convert("RGBA")
    if frac_w is None or frac_h is None:
        return src.resize((size, size), Image.LANCZOS)

    mark = src.crop(src.split()[3].getbbox())
    scale = min(size * frac_w / mark.width, size * frac_h / mark.height)
    mark = mark.resize((max(1, round(mark.width * scale)),
                        max(1, round(mark.height * scale))), Image.LANCZOS)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(mark, ((size - mark.width) // 2, (size - mark.height) // 2))
    return canvas


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

    # The two PWA icons. Checked EXACTLY, on every machine — which is new. While
    # they carried a tile the reference needed the master's gradient stops, and
    # the master is gitignored, so a fresh clone fell back to sniffing whether the
    # backdrop was still a colour the mark uses. Dropping the tile dropped that
    # asymmetry with it: both are now written from tracked pixels, so CI compares
    # the same way a dev box does and there is no CI-only branch left to rot.
    for name, size, fw, fh in PWA_PNGS:
        path = ICONS / name
        rel = path.relative_to(ROOT)
        if not path.exists():
            problems.append(f"{rel} is missing")
            continue
        actual = Image.open(path).convert("RGBA")
        if actual.size != (size, size):
            problems.append(f"{rel} is {actual.size[0]}px, expected {size}px")
            continue
        err = _mae(actual, _pwa_png(size, fw, fh))
        if err > TOLERANCE:
            problems.append(
                f"{rel} is stale against pwa-512.png (mean error {err:.1f}) — "
                "the installed app is on a different logo from the tab")
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

    # NOT gated on the master, unlike favicon.svg above: pwa-512 is tracked and is
    # the only input these need, so `--check` and this branch agree everywhere.
    for name, size, fw, fh in PWA_PNGS:
        path = ICONS / name
        _pwa_png(size, fw, fh).save(path, format="PNG")
        written.append(str(path.relative_to(ROOT)))
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
