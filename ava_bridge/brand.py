"""Branding — the values that make an install look like somebody else's.

Ava ships one look: the VS Code blue `#007acc`, the name rendered as text, and
no logo image anywhere. `brand.name` and `brand.tagline` have existed since the
re-branding seam went in (settings.py, "identity (brand + owner)") but nothing
else did, and neither had a UI. This module is the rest of it.

**Blank means the shipped look, byte for byte.** Every key added here defaults to
the empty string, and empty is not "unset, fall back to a constant" — it is a
statement that this install has not been re-branded, which the CSS reads by
simply never matching its `[data-brand="on"]` rules. That is why the default is a
DATA guarantee rather than a code guarantee: there is no branch to get wrong, and
an install that never opens the Branding panel renders from exactly the token
values it always did.

Pure by design — no FastAPI import, no route, no I/O beyond reading config — so
`tests/test_brand_contrast.py` can pin the calibration numbers without standing
up an app. The routes that use it live in `ava_bridge/hub/branding.py` and
`ava_bridge/pages.py`.

The accessibility guard is the part worth reading twice; see `check_accent`.
"""
from __future__ import annotations

import html as _html
import math
import re

from . import settings

# ---- What can be branded ---------------------------------------------------
# Asset slots are a FIXED enum, and every route that serves or accepts an asset
# takes the slot rather than a filename. A filename in a URL or a config value is
# a path-traversal primitive; a slot is not one, so the bug class does not exist
# rather than being defended against.
SLOTS: tuple[str, ...] = ("logo", "logo_light", "wordmark", "wordmark_light", "icon")

# Colour keys, in the order the panel shows them.
COLOR_KEYS: tuple[str, ...] = ("accent", "accent_light", "chrome")

# The shipped palette, quoted from frontend/src/styles/tokens.css so the panel
# can show "back to Ava's default" without the frontend and the backend each
# keeping their own idea of what that is. Kept in sync by
# tests/test_brand_defaults_match_tokens.py.
DEFAULT_ACCENT = "#007acc"        # tokens.css :root            --accent
DEFAULT_ACCENT_LIGHT = "#006bb3"  # tokens.css [data-theme=light] --accent
DARK_BG = "#262624"               # tokens.css :root            --bg
LIGHT_BG = "#faf9f5"              # tokens.css [data-theme=light] --bg

# Text drawn ON the accent. Hardcoded `color:#fff` in ~15 rules across
# global.css / hub.css / claude.css — including the user's own chat bubble — so
# an accent that white cannot sit on is unreadable in the most-looked-at pixel
# in the app. Changing that to a token is the `--on-accent` follow-up; until it
# exists, white is the constraint and the guard treats it as one.
ON_ACCENT = "#ffffff"

NAME_MAX = 40
TAGLINE_MAX = 140

# Same shape as hub/connectors.py `_COLOR_RE`, minus its `var(--app-accent-N)`
# arm: a palette slot resolves THROUGH the accent, so accepting one here would
# be circular.
ACCENT_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# ---- Colour maths ----------------------------------------------------------
def normalize_hex(value: str | None) -> str:
    """`#abc` -> `#aabbcc`, lowercased. `""` for anything that is not a hex colour.

    Returning `""` rather than raising keeps "no value" and "bad value" the same
    shape for callers that only care whether a colour is usable; the write path
    (`check_accent`) is what refuses a bad value loudly.
    """
    v = (value or "").strip()
    if not ACCENT_RE.match(v):
        return ""
    v = v[1:].lower()
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return f"#{v}"


def _channels(hex_color: str) -> tuple[float, float, float]:
    h = normalize_hex(hex_color)[1:]
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def relative_luminance(hex_color: str) -> float:
    """WCAG 2.x relative luminance."""
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in _channels(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, 1.0 … 21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---- OKLab, for suggesting a fixed colour ----------------------------------
# Lightness is walked in OKLab rather than sRGB because scaling RGB channels
# shifts hue: darkening a saturated blue in sRGB pulls it purple, and a guard
# that answers a brand colour with the wrong hue is a guard people turn off.
def _srgb_to_oklab(hex_color: str) -> tuple[float, float, float]:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in _channels(hex_color))
    # lc/mc/sc: the long/medium/short cone responses, cube-rooted. Named this way
    # rather than the reference implementation's l/m/s because ruff E741 reads a
    # bare `l` as ambiguous with `1`, which in a block of dense coefficients it is.
    lc = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    mc = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    sc = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (0.2104542553 * lc + 0.7936177850 * mc - 0.0040720468 * sc,
            1.9779984951 * lc - 2.4285922050 * mc + 0.4505937099 * sc,
            0.0259040371 * lc + 0.7827717662 * mc - 0.8086757660 * sc)


def _oklab_to_srgb(L: float, a: float, b: float) -> str:
    lc = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    mc = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    sc = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lr = 4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc
    lg = -1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc
    lb = -0.0041960863 * lc - 0.7034186147 * mc + 1.7076147010 * sc

    def enc(c: float) -> int:
        c = max(0.0, min(1.0, c))
        c = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
        return max(0, min(255, round(c * 255)))
    return "#%02x%02x%02x" % (enc(lr), enc(lg), enc(lb))


def suggest(accent: str) -> str:
    """The nearest lightness of the SAME hue that passes both blocking rules.

    Returned alongside a rejection so the panel can offer "#ff3b30 fails on the
    dark canvas — use #ff6b61?" as one click. A guard that only says no is a
    guard whose escape hatch gets used on the first real brand colour, and then
    it protects nothing.

    `""` when no lightness of this hue works, which is the honest answer for a
    hue too close to the canvas at every lightness. The caller shows the
    rejection without a fix rather than inventing one.
    """
    base = normalize_hex(accent)
    if not base:
        return ""
    _, a, b = _srgb_to_oklab(base)
    # 0.02 steps over the usable range; 49 candidates is cheap and finer than
    # the eye resolves at this chroma.
    best, best_margin = "", -1.0
    for i in range(1, 50):
        cand = _oklab_to_srgb(i * 0.02, a, b)
        ok, margins = _accent_rules(cand, cand)
        if ok:
            worst = min(margins)
            if worst > best_margin:
                best, best_margin = cand, worst
    return best


# ---- The accessibility guard ----------------------------------------------
# Calibration, measured against the shipped tokens (tests/test_brand_contrast.py
# pins all three to 2dp, so a refactor that moves them fails loudly):
#
#     #007acc on #ffffff  4.51   text ON the accent
#     #007acc on #262624  3.36   accent ON the dark canvas
#     #006bb3 on #faf9f5  5.31   accent ON the light canvas
#
# Read those numbers together and the trap is obvious. The accent is BOTH a
# background under white text and a foreground on the canvas, and the two pull
# in opposite directions: text-on-accent wants a dark accent, accent-on-canvas
# wants a light one. Ava's own blue threads the needle at 4.51/3.36 — it passes
# AA text by 0.01 and FAILS 4.5:1 against the dark canvas, where the applicable
# rule is the 3:1 non-text bar (WCAG 1.4.11), not 4.5:1.
#
# So a guard that applied 4.5:1 to both would reject the default Ava ships. That
# is not a hypothetical: it is the first thing such a guard would do on a fresh
# install, and it is why these thresholds are asymmetric rather than tidy.
TEXT_ON_ACCENT_MIN = 4.5   # WCAG 1.4.3 AA — white text on the accent
ACCENT_ON_CANVAS_MIN = 3.0  # WCAG 1.4.11 non-text — the accent as a mark


def _accent_rules(accent: str, accent_light: str) -> tuple[bool, list[float]]:
    """(passes, margins). Margin = ratio - threshold, so >= 0 is a pass."""
    return _rules_detail(accent, accent_light)[:2]


def _rules_detail(accent: str, accent_light: str):
    dark = normalize_hex(accent)
    light = normalize_hex(accent_light) or dark
    checks = [
        ("text_on_accent", contrast(dark, ON_ACCENT), TEXT_ON_ACCENT_MIN,
         "white text on your accent would be unreadable"),
        ("accent_on_dark", contrast(dark, DARK_BG), ACCENT_ON_CANVAS_MIN,
         "your accent disappears against the dark canvas"),
        ("accent_on_light", contrast(light, LIGHT_BG), ACCENT_ON_CANVAS_MIN,
         "your light-theme accent disappears against the light canvas"),
    ]
    margins = [ratio - floor for _, ratio, floor, _ in checks]
    return all(m >= 0 for m in margins), margins, checks


def check_accent(accent: str, accent_light: str = "") -> dict:
    """Validate a proposed accent. Blank input is always OK — it means default.

    Shape mirrors what the panel renders: `ok` gates the save, `blocking` is why,
    `warnings` are advisory, `ratios` lets the UI show the numbers rather than a
    verdict, and `suggest` is the one-click fix.
    """
    out: dict = {"ok": True, "blocking": [], "warnings": [], "ratios": {},
                 "suggest": ""}
    dark = normalize_hex(accent)
    if not accent.strip():
        return out                      # blank = Ava's default, nothing to check
    if not dark:
        out["ok"] = False
        out["blocking"].append("accent must be a #rrggbb colour")
        return out
    if accent_light.strip() and not normalize_hex(accent_light):
        out["ok"] = False
        out["blocking"].append("accent_light must be a #rrggbb colour")
        return out

    ok, _margins, checks = _rules_detail(accent, accent_light)
    for key, ratio, floor, why in checks:
        out["ratios"][key] = round(ratio, 2)
        if ratio < floor:
            out["blocking"].append(f"{why} ({ratio:.2f}:1, needs {floor}:1)")
    light = normalize_hex(accent_light) or dark
    on_white_light = contrast(light, ON_ACCENT)
    out["ratios"]["text_on_accent_light"] = round(on_white_light, 2)
    if on_white_light < TEXT_ON_ACCENT_MIN:
        out["warnings"].append(
            f"white text on the light-theme accent is {on_white_light:.2f}:1, "
            f"below the {TEXT_ON_ACCENT_MIN}:1 bar for body text")
    out["ok"] = ok
    if not ok:
        out["suggest"] = suggest(accent)
    return out


# ---- Config accessors ------------------------------------------------------
def name() -> str:
    return settings.brand_name()


def tagline() -> str:
    return settings.brand_tagline()


def accent() -> str:
    return settings.brand_accent()


def accent_light() -> str:
    """The light-theme accent. Blank falls back to `accent`, NOT to Ava's
    default: an install that set one colour means that colour in both themes,
    and silently reverting to blue on light would read as a bug."""
    return settings.brand_accent_light() or settings.brand_accent()


def chrome() -> str:
    return settings.brand_chrome()


def is_branded() -> bool:
    """Whether anything has been re-branded beyond the name/tagline that shipped.

    Drives the `data-brand="on"` stamp: false means the CSS never matches its
    override block and the install renders from the shipped tokens untouched.
    """
    return bool(accent() or chrome() or any(asset_set(s) for s in SLOTS))


def asset_name(slot: str) -> str:
    """The stored FILENAME for a slot, or `""`. Never a path — see SLOTS."""
    if slot not in SLOTS:
        return ""
    raw = settings.brand_asset(slot)
    # Defence in depth: config is owner-controlled today, but a brand pack makes
    # it third-party-controlled, and this value is joined onto a directory.
    if not raw or "/" in raw or "\\" in raw or raw.startswith("."):
        return ""
    return raw


def asset_set(slot: str) -> bool:
    return bool(asset_name(slot))


def accent_hover(base: str = "") -> str:
    """The pressed/hover shade: the accent mixed 86% toward black.

    86% is not arbitrary — it reproduces Ava's shipped pair to within 2/255 in
    both themes (#007acc -> #0069b0 vs the shipped #0068ad), and it is the same
    figure the `[data-brand]` CSS block uses via
    `color-mix(in srgb, var(--accent) 86%, #000)`. One number, two
    implementations, so a branded page and a branded SPA cannot disagree.
    """
    h = normalize_hex(base or accent())
    if not h:
        return ""
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (round(r * 0.86), round(g * 0.86), round(b * 0.86))


# ---- The server-rendered pre-auth surfaces ---------------------------------
# /login, /setup and /setup/wizard are plain HTML outside React, and each carried
# its own byte-identical copy of the token block below. Three copies drift for
# free, and they all hardcoded #007acc — so a re-branded install had a blue
# sign-in page and a branded app. One function now emits it for all three, WITH
# the live accent already resolved, which means those pages follow the brand
# server-side: no client JS, no cached value, and therefore no flash at all.
def pre_auth_css() -> str:
    """The `:root` token block the three server-rendered pages share.

    When nothing is branded this emits the shipped literals byte for byte, so
    the default sign-in page is pixel-identical to the one that shipped. Only a
    branded install takes the derived path.
    """
    a = normalize_hex(accent()) if brand_is_public() else ""
    hover = accent_hover(a) if a else ""
    bg = (normalize_hex(chrome()) if brand_is_public() else "") or "#262624"
    return (
        ":root{--bg:%s;--panel:#1f1e1d;--line:#3b3a36;--muted:#9a968c;--txt:#f4f3ee;\n"
        "   --accent:%s;--accent-hover:%s;--input:#30302e;--inputline:#46443f;\n"
        '   --serif:ui-serif,Georgia,"Times New Roman",serif}'
    ) % (bg, a or DEFAULT_ACCENT, hover or "#0068ad")


# The mark that shipped: a stroke-only robot glyph, duplicated verbatim in
# login.html and setup.html. Kept here as the fallback so there is one copy.
_SHIPPED_MARK = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18"'
    ' height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/>'
    '<line x1="8" y1="16" x2="8" y2="16"/><line x1="16" y1="16" x2="16" y2="16"/></svg>'
)


def pre_auth_mark() -> str:
    """The logo for the sign-in card: the owner's image, or the shipped glyph."""
    if brand_is_public() and asset_set("logo"):
        return ('<img src="/brand/asset/logo" alt="" width="28" height="28" '
                'style="border-radius:6px;object-fit:contain">')
    return _SHIPPED_MARK


def brand_is_public() -> bool:
    return settings.brand_public()


def pre_auth_chrome() -> str:
    """The <meta name="theme-color"> value for the pre-auth pages."""
    return (normalize_hex(chrome()) if brand_is_public() else "") or "#262624"


def pre_auth_name() -> str:
    """The name shown before sign-in. `brand.public: false` renders Ava's own
    name rather than a blank — a blank card announces that something is being
    hidden, which is worse than the default it is trying to conceal."""
    return name() if brand_is_public() else "Ava"


def pre_auth_tagline() -> str:
    return tagline() if brand_is_public() else ""


def render_page(tmpl: str, msg: str = "") -> str:
    """Substitute the brand sentinels in a server-rendered page, at REQUEST time.

    Lives here rather than in pages.py because all three consumers
    (`/login`, `/setup`, `/setup/wizard`) are in two different modules, and
    putting it in one of them would make the other import a page router to
    render a colour.

    `html.escape` on every TEXT substitution. The name is owner-controlled
    today, but a brand pack makes it third-party controlled and it lands inside
    a <title> and an <h1>. The CSS and mark are not escaped because they are
    markup this module generates from already-validated values — every colour
    goes through `normalize_hex`, and the mark is a fixed literal or an <img>
    whose src is a fixed slot URL. Nothing user-supplied reaches them as markup.
    """
    return (tmpl.replace("__BRAND_CSS__", pre_auth_css())
                .replace("__BRAND_MARK__", pre_auth_mark())
                .replace("__BRAND_CHROME__", pre_auth_chrome())
                .replace("__BRAND__", _html.escape(pre_auth_name()))
                .replace("<!--MSG-->", msg))


# ---- Asset ingest ----------------------------------------------------------
# **SVG is refused, not sanitised.** Ava serves assets from its own origin and
# sets no Content-Security-Policy anywhere (`grep -rn Content-Security-Policy`
# over the tree returns nothing), so an uploaded SVG opened at /brand/asset/logo
# would execute its own <script> with the session cookie — stored XSS with full
# API access, from a one-click upload in Setup. `media_api._ALLOWED_UPLOAD_EXTS`
# already omits .svg deliberately; re-admitting it in a *branding* feature would
# be inconsistent and unreviewed. Raster covers every slot anyway — PWA icons
# must be raster. If an SVG master is ever needed it belongs INSIDE a brand pack,
# rasterised on ingest, never served as image/svg+xml.
MAX_ASSET_BYTES = 2 * 1024 * 1024
ALLOWED_FORMATS = frozenset({"PNG", "WEBP", "JPEG"})
MIN_PX, MAX_PX = 64, 4096

# The sizes derived from the one `icon` source. Names double as the slot in the
# serving URL, so nothing here is a filename either.
DERIVED = {
    "favicon": 48,
    "pwa-192": 192,
    "pwa-512": 512,
    "pwa-maskable-512": 512,
    "apple-touch-icon": 180,
}
_MASKABLE_SAFE_PAD = 0.10   # 10% each side — the maskable safe-zone convention


def _pillow():
    from PIL import Image  # imported lazily: brand.py must stay import-cheap
    return Image


def ingest_asset(slot: str, raw: bytes) -> tuple[str, str]:
    """(stored_filename, error). Never trusts the caller's filename or MIME type.

    The single most valuable step here is the RE-ENCODE. The bytes written are
    bytes Pillow produced, not the bytes that were uploaded — which strips EXIF,
    any payload appended after the image data (the classic polyglot), and any
    ICC/metadata trickery, and makes "is this really a PNG" moot rather than a
    question to answer. tests/test_brand_assets.py asserts served != uploaded
    explicitly, because that property is easy to optimise away by accident.
    """
    if slot not in SLOTS:
        return "", f"unknown slot '{slot}'"
    if not raw:
        return "", "empty upload"
    if len(raw) > MAX_ASSET_BYTES:
        return "", f"image is larger than {MAX_ASSET_BYTES // (1024 * 1024)} MB"

    Image = _pillow()
    import io
    try:
        # verify() then reopen: verify() invalidates the object, and load() is
        # what actually raises DecompressionBombError on a pixel bomb.
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:  # noqa: BLE001 — any decode failure is a refusal
        name = type(e).__name__
        if "DecompressionBomb" in name:
            return "", "image dimensions are implausibly large"
        return "", "that file is not a readable PNG, WebP or JPEG"

    if img.format not in ALLOWED_FORMATS:
        return "", (f"{img.format or 'that format'} is not supported — "
                    "use PNG, WebP or JPEG (SVG is deliberately not accepted)")
    w, h = img.size
    if min(w, h) < MIN_PX:
        return "", f"image is smaller than {MIN_PX}x{MIN_PX}"
    if max(w, h) > MAX_PX:
        return "", f"image is larger than {MAX_PX}x{MAX_PX}"

    out = io.BytesIO()
    img.convert("RGBA").save(out, format="PNG", optimize=True)
    data = out.getvalue()

    import hashlib
    import os
    import tempfile
    d = settings.brand_dir()
    os.makedirs(d, exist_ok=True)
    # Content-addressed so a re-upload never collides with a cached response and
    # the old file is replaceable rather than overwritten in place.
    fname = f"{slot}-{hashlib.sha256(data).hexdigest()[:12]}.png"
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)          # served, not secret
        os.replace(tmp, os.path.join(d, fname))
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return "", f"could not store the image: {e}"
    _clear_derived()
    return fname, ""


def _clear_derived() -> None:
    """Drop the derived-icon cache. Cheap to rebuild, wrong to keep."""
    import os
    import shutil
    p = os.path.join(settings.brand_dir(), "derived")
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)


def icon_source() -> str:
    """Which uploaded file the icon set is derived from: `icon`, else `logo`.

    Falling back to the logo means one upload is enough to re-brand the
    home-screen tile, which is the thing people forget to do and then notice on
    their phone a week later.
    """
    return asset_name("icon") or asset_name("logo")


def derived_icon(name: str) -> str:
    """Absolute path to a derived icon, building it on first use. '' if there is
    no source image or the render fails."""
    import os
    size = DERIVED.get(name)
    src_name = icon_source()
    if size is None or not src_name:
        return ""
    d = settings.brand_dir()
    src = os.path.join(d, src_name)
    if not os.path.isfile(src):
        return ""
    out_dir = os.path.join(d, "derived")
    out = os.path.join(out_dir, f"{name}.png")
    if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    Image = _pillow()
    try:
        os.makedirs(out_dir, exist_ok=True)
        img = Image.open(src)
        img.load()
        img = img.convert("RGBA")
        if name == "pwa-maskable-512":
            # A maskable icon is cropped to a circle by the launcher, so the
            # artwork has to sit inside a safe zone or the mark loses its edges.
            inner = int(size * (1 - 2 * _MASKABLE_SAFE_PAD))
            art = img.resize((inner, inner), Image.LANCZOS)
            canvas = Image.new("RGBA", (size, size), _maskable_bg())
            off = (size - inner) // 2
            canvas.paste(art, (off, off), art)
            img = canvas
        else:
            img = img.resize((size, size), Image.LANCZOS)
        img.save(out, format="PNG", optimize=True)
        return out
    except Exception:  # noqa: BLE001 — a bad source must not 500 the manifest
        return ""


def _maskable_bg() -> tuple[int, int, int, int]:
    h = normalize_hex(chrome()) or DARK_BG
    return (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16), 255)


def discard_file(fname: str, *, slot: str, reason: str) -> None:
    """Delete one stored brand image, and record that it happened.

    The audit call is HERE rather than in the route above, which is the rule
    tests/test_destructive_paths_audited.py enforces and the reason it exists:
    an audited route over an unaudited store is how the most frequent deletion
    path on the box became invisible. Both callers — clearing a slot, and
    dropping the file a slot no longer points at after a replace — destroy an
    image the owner uploaded, so both get a row.
    """
    import os

    from . import audit
    if not fname:
        return
    path = os.path.join(settings.brand_dir(), fname)
    existed = os.path.isfile(path)
    try:
        os.unlink(path)
    except OSError:
        existed = False
    if existed:
        audit.record("brand_asset_delete", slot=slot, file=fname, reason=reason)


def clear_asset(slot: str) -> None:
    """Clear a slot's image. The config key is cleared by the caller."""
    discard_file(asset_name(slot), slot=slot, reason="cleared")
    _clear_derived()


def payload() -> dict:
    """The public brand description — what `/api/brand` returns and what the
    frontend caches for its pre-paint stamp.

    Assets are reported as booleans plus a stable slot URL, never as filenames:
    the client never needs the name, and not sending it keeps the only copy on
    the server.
    """
    return {
        "name": name(),
        "tagline": tagline(),
        "accent": accent(),
        "accent_light": accent_light(),
        "chrome": chrome(),
        "branded": is_branded(),
        "assets": {s: (f"/brand/asset/{s}" if asset_set(s) else "") for s in SLOTS},
    }
