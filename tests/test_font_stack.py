"""One typeface across the app, the pre-auth pages and the docs site.

The product had SIX competing font declarations — a `--serif` in tokens.css, a
second one hardcoded in ava_bridge/brand.py, a body stack inlined in claude.css
that shadowed the (referenced-by-nothing) --font-ui token, another in the legacy
page, and a docs-site stack that named `Inter` while `font: false` meant nothing
ever served it. The visible symptom was headings in Times over paragraphs in
whatever sans the visitor's OS happened to supply.

Two failure modes put that back, and neither shows up in review:

  1. Naming a family nobody serves. `Inter` at the head of a stack looks correct
     in the stylesheet and silently renders as the OS default for every visitor
     who has not installed it. A font is only real if an @font-face points at a
     file that exists.
  2. Declaring a SECOND family for headings. That is what makes a title disagree
     with its own paragraph, and a bare `Georgia` or `ui-serif` is how it got in.

These are static scans over tracked files: no browser, no build, runs in CI.
"""
import pathlib
import re

from gitfiles import tracked_paths as _tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

FONT_DIR = ROOT / "frontend/public/fonts"
FAMILY = '"Inter Variable"'
FILES = ("inter-latin-wght-normal.woff2", "inter-latin-wght-italic.woff2")

# Every surface that declares an @font-face for the shared typeface. They cannot
# share one file: the app is mounted at "/", the docs site is published under
# /Ava/, and the pre-auth pages ship as a single self-contained <style> with no
# build step. So the declaration is duplicated on purpose and pinned here.
FACE_SITES = (
    "frontend/src/styles/fonts.css",
    "docs-site/stylesheets/extra.css",
    "ava_bridge/brand.py",
    "ava_bridge/web/index.html",
)

# Where a second family would do visible damage. Restricted to stylesheets and
# the served pages — prose in docs/ is free to mention Georgia.
STYLE_GLOBS = ("frontend/src/styles/*.css", "docs-site/stylesheets/*.css",
               "ava_bridge/web/*.html")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


_COMMENT = re.compile(r"/\*.*?\*/|<!--.*?-->", re.S)


def _decommented(body: str) -> str:
    """Blank out comments, preserving line numbers.

    The guard below scans for family names, and the files it scans EXPLAIN at
    length why those names are banned — so a naive scan flags its own
    documentation and the only way to keep it green is to stop writing the
    comments. Newlines are kept so reported line numbers still point at the
    real line.
    """
    return _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), body)


def test_the_font_files_are_actually_in_the_repo() -> None:
    """A stack that names a family nobody ships is the original bug."""
    missing = [f for f in FILES if not (FONT_DIR / f).is_file()]
    assert not missing, (
        f"missing {missing} in frontend/public/fonts/ — every @font-face in "
        f"{list(FACE_SITES)} points at them, and without the files each surface "
        "falls back to the OS default with no error anywhere"
    )
    for f in FILES:
        head = (FONT_DIR / f).read_bytes()[:4]
        assert head == b"wOF2", f"{f} is not a woff2 (magic {head!r})"


def test_the_licence_travels_with_the_fonts() -> None:
    """SIL OFL 1.1 requires it, and both the app and the site redistribute."""
    ofl = FONT_DIR / "OFL.txt"
    assert ofl.is_file(), "frontend/public/fonts/OFL.txt is missing"
    assert "SIL Open Font License" in ofl.read_text(encoding="utf-8")


def test_every_surface_declares_the_same_family_and_files() -> None:
    """Four copies of one declaration; drift makes one surface silently differ."""
    for rel in FACE_SITES:
        body = _read(rel)
        assert "@font-face" in body, f"{rel} lost its @font-face"
        assert FAMILY in body, f"{rel} does not name {FAMILY}"
        # The roman is required everywhere; only the surfaces that set italic
        # type need the italic file, so it is not asserted per-site here.
        assert FILES[0] in body, f"{rel} does not point at {FILES[0]}"


def test_no_surface_reintroduces_a_second_family() -> None:
    """The heading/body mismatch, caught at its source.

    `sans-serif` and `ui-monospace` are fine — they are fallback keywords, not a
    second typeface. A serif family is not.
    """
    banned = re.compile(r"--serif\b|ui-serif|\bGeorgia\b|Times New Roman")
    offenders = []
    for glob in STYLE_GLOBS:
        for p in _tracked(glob):
            src = _decommented(p.read_text(encoding="utf-8"))
            for i, line in enumerate(src.splitlines(), 1):
                if banned.search(line):
                    offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "a second (serif) family is back:\n  " + "\n  ".join(offenders) +
        "\nHierarchy is weight + size + tracking within one family — see the "
        "Typography block in frontend/src/styles/tokens.css. If a display face "
        "is genuinely wanted, self-host it and add it to tokens.css; never a "
        "bare Georgia/ui-serif, which resolves to Times on Windows."
    )


def test_python_and_css_agree_on_the_pre_auth_stack() -> None:
    """brand.pre_auth_css() is the app's tokens.css restated in Python."""
    assert FAMILY in _read("ava_bridge/brand.py")
    assert FAMILY in _read("frontend/src/styles/tokens.css")
    assert "--font-ui" in _read("ava_bridge/brand.py"), (
        "the pre-auth pages style themselves with var(--font-ui); brand.py is "
        "the only thing that defines it for them"
    )


def test_the_fonts_are_reachable_before_login() -> None:
    """Gated, they 303 to /setup and the sign-in card silently falls back."""
    auth = _read("ava_bridge/auth.py")
    for f in FILES:
        assert f"/fonts/{f}" in auth, (
            f"/fonts/{f} is not in auth._PUBLIC_PATHS — the sign-in, setup and "
            "claim cards render before any cookie exists"
        )
    # The route that answers those paths validates against a fixed enum rather
    # than joining a caller-supplied filename.
    pages = _read("ava_bridge/pages.py")
    for f in FILES:
        assert f in pages, f"no /fonts route serves {f}"


def test_the_body_font_is_read_from_the_token_not_restated() -> None:
    """claude.css:11 used to inline the stack, leaving --font-ui referenced by
    nothing and the real value living in a rule instead of the SSOT."""
    claude = _read("frontend/src/styles/claude.css")
    assert re.search(r"body\s*\{\s*font-family:\s*var\(--font-ui\)", claude), (
        "claude.css must take the body font from var(--font-ui), not restate it"
    )


def test_the_docs_site_inherits_the_app_families() -> None:
    """The site follows tokens.css the way it already follows the palette."""
    extra = _read("docs-site/stylesheets/extra.css")
    assert "--md-text-font-family: var(--font-ui)" in extra
    assert "--md-code-font-family: var(--font-mono)" in extra


def test_sync_stages_the_fonts_into_the_site() -> None:
    """Without this the published @font-face 404s and the site falls back."""
    sync = _read("docs-site/sync.py")
    for f in FILES + ("OFL.txt",):
        assert f"docs/assets/fonts/{f}" in sync, (
            f"sync.py does not stage {f} — docs-site/stylesheets/extra.css "
            "points at docs/assets/fonts/"
        )
