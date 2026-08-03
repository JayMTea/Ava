"""Render a .d2 source to SVG, and stamp it so freshness is provable offline.

Extracted from `agent/docs/arch.py` so the render + stamp path has exactly one
implementation: find the binary, render, optionally place the result on a fixed
sheet, and stamp the source hash into the output. `arch.py` keeps thin private
aliases, so none of its call sites changed.

Deliberately manifest-agnostic: a caller resolves its own style tokens into a
plain dict and passes them in. Nothing here knows what a manifest is, which is
what lets more than one diagram pipeline share it without either inheriting the
other's schema.

**The stamp is the point.** `stamp()` appends `<!-- d2sum:<sha256 of the .d2> -->`
after `</svg>`, so any consumer — `check_drift`, `tests/test_diagram_sync.py`, CI
on a machine with no `d2` installed — can prove an SVG was rendered from the
current source by hashing a file. Two consequences worth knowing:

  * the comment sits OUTSIDE the root element, so a reader must parse the file as
    HTML (`innerHTML`) rather than as XML (`DOMParser(..., 'image/svg+xml')`),
    where trailing content after the root is a fatal error;
  * `tests/test_diagram_sync.py` re-implements the sha256 independently rather
    than importing `d2sum()` — on purpose, so the guard does not trust the code
    it guards. Do not "fix" that duplication.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess

# Renderer-level defaults. Only these five keys are read; a caller's richer style
# dict (arch.py's carries stroke widths and font sizes for its diagram BUILDERS)
# passes through untouched.
#
# `layout` is elk and should stay elk: TALA stamps an unlicensed-copy watermark
# into the SVG, three tracked diagrams shipped watermarked for weeks, and
# tests/test_no_owner_identity.py now fails the build on that string.
#
# Hyphenated deliberately. Spelled as the watermark spells it, this comment IS a
# match for the pattern policing it, and the guard fails on the file that explains
# the rule — which is how it first fired here. test_no_owner_identity.py dodges the
# same trap by assembling the literal from two pieces.
DEFAULT_STYLE: dict = dict(theme=0, layout="elk", pad=24, sheet=None,
                           background="#ffffff")


def binary() -> str:
    """Path to the d2 binary (may not exist — callers check)."""
    return shutil.which("d2") or os.path.expanduser("~/.local/bin/d2")


def d2sum(d2_path: str) -> str:
    """sha256 of the .d2 source."""
    with open(d2_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def svg_d2sum(svg_path: str) -> str | None:
    """The d2sum stamped into the SVG at render time (None if unstamped)."""
    try:
        with open(svg_path, encoding="utf-8") as f:
            m = re.search(r"d2sum:([0-9a-f]{64})", f.read())
        return m.group(1) if m else None
    except OSError:
        return None


def stamp(d2_path: str, svg_path: str) -> None:
    """Stamp the source hash so freshness is checkable without d2 installed."""
    with open(svg_path, "a", encoding="utf-8") as f:
        f.write(f"<!-- d2sum:{d2sum(d2_path)} -->\n")


def fit_to_sheet(svg_path: str, style: dict) -> None:
    """Place the rendered diagram on a fixed-size sheet (e.g. 11x17 ANSI-B).

    D2 sizes the SVG to its content, so diagrams come out at different sizes.
    For uniform docs/presentations we wrap the diagram in an outer SVG of fixed
    sheet dimensions and scale it to fit (centered, white margins) via a nested
    <svg> with preserveAspectRatio. Vector-clean and reproducible; controlled by
    the caller's `sheet` style token (omit it to keep content-sized).

    Note for anyone reading the result programmatically: this adds an OUTER
    <svg>, so `querySelector('svg')` then returns the wrapper rather than the
    diagram. Sheet-fit only what is meant to be printed.
    """
    sheet = style.get("sheet")
    if not sheet:
        return
    pw, ph = int(sheet["px_w"]), int(sheet["px_h"])
    win, hin = sheet["w_in"], sheet["h_in"]
    margin = int(sheet.get("margin", 0))
    bg = style.get("background", "#ffffff")
    with open(svg_path, encoding="utf-8") as f:
        svg = f.read()
    mo = re.search(r"<svg\b[^>]*>", svg, re.S)
    if not mo:
        return
    open_tag = mo.group(0)
    vb = re.search(r'viewBox="([^"]+)"', open_tag)
    if vb:
        view_box = vb.group(1)
    else:
        w = re.search(r'width="([\d.]+)', open_tag)
        h = re.search(r'height="([\d.]+)', open_tag)
        if not (w and h):
            return
        view_box = f"0 0 {w.group(1)} {h.group(1)}"
    # Re-size the diagram's own <svg> to fill the sheet's inner box and centre it.
    inner = re.sub(r'\s(?:width|height|preserveAspectRatio)="[^"]*"', "", open_tag)
    if 'viewBox="' not in inner:
        inner = inner[:-1] + f' viewBox="{view_box}"'
    iw, ih = pw - 2 * margin, ph - 2 * margin
    inner = (inner[:-1] +
             f' x="{margin}" y="{margin}" width="{iw}" height="{ih}"'
             f' preserveAspectRatio="xMidYMid meet">')
    nested = inner + svg[mo.end():]
    prefix = svg[:mo.start()]
    wrapper = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{win}in" height="{hin}in" viewBox="0 0 {pw} {ph}">'
        f'<rect x="0" y="0" width="{pw}" height="{ph}" fill="{bg}"/>'
        f'{nested}</svg>\n'
    )
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(prefix + wrapper)


def render(d2_path: str, svg_path: str, style: dict | None = None) -> None:
    """Render, optionally sheet-fit, and stamp. Raises if d2 is not installed."""
    st = {**DEFAULT_STYLE, **(style or {})}
    exe = binary()
    if not os.path.exists(exe):
        raise RuntimeError("d2 not found (install: https://d2lang.com/install.sh)")
    subprocess.run(
        [exe, "--theme", str(st["theme"]), "--layout", str(st["layout"]),
         "--pad", str(st["pad"]), d2_path, svg_path],
        check=True, capture_output=True, text=True,
    )
    fit_to_sheet(svg_path, st)
    stamp(d2_path, svg_path)
