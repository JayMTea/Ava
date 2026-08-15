"""The landing page is a Jinja template, and almost nothing else checks it.

docs-site/overrides/home.html is the whole homepage now: six bands, no
markdown body. That buys composed full-bleed layout and costs every check the
markdown pipeline was doing for free, because `mkdocs build --strict` validates
links in MARKDOWN, not `{{ ... | url }}` in a template, and MkDocs never
verifies that a file named in extra_javascript exists at all. Meanwhile sync.py
copies the WORKING TREE rather than `git ls-files`, so an untracked file
previews perfectly and 404s in production, where every guard scans tracked
files only.

Net: several ways to break the site's most-visited page while `--strict` stays
green and every existing test passes. This file closes the ones a static scan
can reach. It needs no browser, no build and no network, in the style of
tests/test_diagram_sync.py.

What it deliberately does NOT claim:
  - that the figure is a GOOD or TRUE picture of the product. No static scan
    knows that eight tiles converging describes this software, and no scan can
    see that a drawing resembles something it should not. A drawing has no
    strings. That judgement stays with a human reading a diff.
  - that anything clears AA, or renders at all. Both need a browser.

Run: python -m pytest tests/test_landing_page.py -q
"""
import ast
import pathlib
import re

from gitfiles import require_git, tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "docs-site"
HOME = SITE / "overrides" / "home.html"
EXTRA_CSS = SITE / "stylesheets" / "extra.css"
SYNC = SITE / "sync.py"
MKDOCS = SITE / "mkdocs.yml"
TOKENS = ROOT / "frontend" / "src" / "styles" / "tokens.css"
ICONS_TSX = ROOT / "frontend" / "src" / "lib" / "icons.tsx"

# The tile set is pinned here rather than inferred. This does not prove the
# picture is honest; it makes the picture impossible to change without editing
# a test whose docstring says what the rule is. That is the same thing a
# d2sum stamp buys in test_diagram_sync.py - a forced stop, not a proof.
#
# These eight are the apps a person BUILDS for themselves, which is the claim
# the page makes. They were Calendar / Mail / Docs / Code / Notes / Photos /
# Contacts / Devices, the stock icon grid every assistant on the market draws,
# and that set argued the commodity claim instead. If you change them again,
# read test_tile_set_is_pinned_and_names_no_shipped_connector below first.
_TILES = frozenset({"Health", "Finance", "Clients", "Blog",
                    "Inventory", "Photos", "Notes", "Home"})

_INCLUDE = re.compile(r"""\{%-?\s*include\s+"\.icons/ava/([A-Za-z]+)\.svg"\s*-?%\}""")
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_TILE_EL = re.compile(r"<li class=\"ava-conv__tile\"[^>]*>")
_TILE_LABEL = re.compile(r'<span class="ava-conv__label">([^<]+)</span>')
_ACCENT_USE = re.compile(r"--tile-accent:\s*var\(--app-accent-(\d)\)")
_IMG_SVG = re.compile(r"<img[^>]+src=[^>]*\.svg", re.I)
# {{ 'docs/assets/ava-tour.mp4' | url }} — the template's only way of naming a
# staged file. Pages are written the same way but end in "/", which is what
# separates a nav destination from an asset here.
_URL_REF = re.compile(r"\{\{\s*'([^']+)'\s*\|\s*url\s*\}\}")
_VIDEO_EL = re.compile(r"<video\b", re.I)
_VAR_USE = re.compile(r"var\(\s*(--[a-zA-Z0-9-]+)\s*(\)|,)")
_DECLARES = re.compile(r"^\s*(--[a-zA-Z0-9-]+)\s*:", re.M)


def _home() -> str:
    """The template with its {# ... #} comments removed.

    These files carry long why-comments that quote the very markup they warn
    against - "do not turn this into <img src=...svg>" - so a scan over the raw
    text flags the warning as the offence.
    """
    return _JINJA_COMMENT.sub("", HOME.read_text(encoding="utf-8"))


def _landing_css() -> str:
    """The figure's CSS, comments stripped, for the same reason as _home()."""
    css = EXTRA_CSS.read_text(encoding="utf-8")
    return _CSS_COMMENT.sub("", css[css.index("/* The convergence figure"):])


def _sync_value(name: str):
    """Read a tuple/dict literal out of sync.py without importing it."""
    tree = ast.parse(SYNC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets = getattr(node, "targets", []) or ([node.target] if getattr(node, "target", None) else [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {SYNC.relative_to(ROOT)}")


def _sync_literal(name: str) -> list[str]:
    """`_sync_value()` flattened to the names a guard usually wants."""
    value = _sync_value(name)
    return list(value.keys()) if isinstance(value, dict) else list(value)


def test_every_landing_glyph_is_staged_and_still_exists() -> None:
    """A glyph renamed in the app ships stale here, forever, on a green build.

    sync.py's _stage_icons() only APPENDS to `missing`; main() prints a warning
    and returns 0. And overrides/.icons/ava/*.svg are TRACKED, so the stale file
    stays on disk and the page keeps rendering last month's drawing. Nothing
    else in the tree notices.
    """
    require_git()
    used = set(_INCLUDE.findall(_home()))
    assert used, "the landing template includes no glyphs - did the figure change shape?"

    wanted = set(_sync_literal("ICONS_WANTED"))
    unstaged = sorted(used - wanted)
    assert not unstaged, (
        f"home.html includes glyphs that sync.py does not stage: {unstaged}\n"
        f"Add them to ICONS_WANTED in {SYNC.relative_to(ROOT)}."
    )

    src = ICONS_TSX.read_text(encoding="utf-8")
    gone = sorted(n for n in used if not re.search(r"^\s{2}%s:" % re.escape(n), src, re.M))
    assert not gone, (
        f"the landing figure uses glyphs that no longer exist in the app: {gone}\n"
        f"{ICONS_TSX.relative_to(ROOT)} is the SSOT. Either restore the glyph or "
        f"change the tile in {HOME.relative_to(ROOT)}."
    )


def test_tile_set_is_pinned_and_names_no_shipped_connector() -> None:
    """The tiles illustrate a mechanism; they must not read as a catalog.

    Ava ships no app integrations - the tracked connectors are infrastructure -
    so a tile naming a real one would turn an illustration into a claim about
    software that does not exist.
    """
    require_git()
    labels = set(_TILE_LABEL.findall(_home()))
    assert labels == _TILES, (
        f"the landing figure's tiles changed.\nexpected {sorted(_TILES)}\n"
        f"found    {sorted(labels)}\n"
        "If that is deliberate, update _TILES here - and read this test's "
        "docstring first, because the tiles are generic categories on purpose."
    )

    names = set()
    for rel in tracked("connectors/"):
        if not rel.endswith("connector.yaml"):
            continue
        for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:id|label):\s*(.+?)\s*$", line)
            if m:
                names.add(m.group(1).strip('"\' ').lower())
    clash = sorted(lbl for lbl in labels if lbl.lower() in names)
    assert not clash, (
        f"landing tiles name a shipped connector: {clash}\n"
        "The tiles stand for apps the VISITOR already runs, not for integrations "
        "Ava provides. Rename the tile."
    )


def test_tile_accents_use_every_palette_slot_exactly_once() -> None:
    """Eight tiles, eight identity accents, no literals and no collisions.

    appAccent() hashes an id at runtime because it must handle arbitrary ones;
    drawing eight from eight would collide almost every time, putting two tiles
    in one colour and misrepresenting what the palette is for. Using each slot
    once is the faithful depiction. tests/test_brand_tokens.py guards the
    shipped BLUE and says nothing about these eight.
    """
    # Scoped to the tiles: the hub's chips reuse three of the same accents on
    # purpose, and counting those would make the set look duplicated.
    tile_slots = sorted(s for el in _TILE_EL.findall(_home())
                        for s in _ACCENT_USE.findall(el))
    assert tile_slots == [str(i) for i in range(8)], (
        f"expected --app-accent-0..7 used once each across the tiles, got {tile_slots}"
    )

    declared = set(_DECLARES.findall(TOKENS.read_text(encoding="utf-8")))
    missing = sorted(f"--app-accent-{i}" for i in range(8)
                     if f"--app-accent-{i}" not in declared)
    assert not missing, (
        f"the app palette no longer declares {missing}\n"
        f"{TOKENS.relative_to(ROOT)} is the SSOT and the figure reads straight from it."
    )


def test_landing_css_references_no_undeclared_custom_property() -> None:
    """A var() pointing at a deleted property is not an error - it is invisible.

    It computes to the unset value: transparent fills, missing borders,
    black-on-black. On a green build, on the LCP page. And the rename that
    causes it lands in a frontend PR that never touches docs-site/, because the
    site deliberately inherits a palette it does not own.
    """
    css = _CSS_COMMENT.sub("", EXTRA_CSS.read_text(encoding="utf-8"))
    # A per-instance value like --tile-accent is declared on the element in
    # home.html's style attribute, which is exactly how the figure gives each
    # tile its own identity accent without a literal.
    inline = set(re.findall(r"(--[a-zA-Z0-9-]+)\s*:", " ".join(_TILE_EL.findall(_home()) + _home().split('style="')[1:])))
    declared = (set(_DECLARES.findall(css))
                | set(_DECLARES.findall(TOKENS.read_text(encoding="utf-8")))
                | inline)
    # Material's own variables are declared in the theme bundle, not here.
    used = {n for n, _ in _VAR_USE.findall(css) if not n.startswith("--md-")}
    # A var() with a fallback survives a missing declaration by design.
    with_fallback = {n for n, tail in _VAR_USE.findall(css) if tail == ","}
    undeclared = sorted(used - declared - with_fallback)
    assert not undeclared, (
        f"extra.css uses custom properties nothing declares: {undeclared}\n"
        f"Either declare them here or check they still exist in {TOKENS.relative_to(ROOT)}."
    )


def test_landing_motion_rests_and_honours_reduced_motion() -> None:
    """The figure is a still image that assembles itself once.

    Two regressions this catches: a debug `infinite` left in, and a new
    animated element added without extending the reduced-motion block. The
    second is silent to the author and harmful only to the user who asked for
    less motion.
    """
    conv = _landing_css()

    assert "infinite" not in conv, (
        "the landing figure animates forever. It rests after ~1.8s by design, "
        "and perpetual motion next to body copy is why people scroll past a hero."
    )
    assert "@media (prefers-reduced-motion: reduce)" in conv, (
        "the figure animates with no reduced-motion block. The house convention "
        "is a trailing @media immediately after the animated rules - see "
        "frontend/src/styles/global.css and hub.css."
    )

    reduce_block = conv[conv.index("@media (prefers-reduced-motion: reduce)"):]
    uncovered = []
    for sel, body in re.findall(r"([^{}]*)\{([^}]*)\}", conv[: conv.index("@media")]):
        if "animation:" not in body:
            continue
        classes = re.findall(r"\.ava-conv__[a-z]+", sel)
        # :is() matches descendants, so naming any class in the selector covers
        # the rule. A rule with no .ava-conv__* class is the container itself.
        if classes and not any(c in reduce_block for c in classes):
            uncovered.append(sel.strip())
    assert not uncovered, (
        f"these animate but the reduced-motion block does not name them: {uncovered}"
    )


def test_landing_media_is_staged_and_tracked() -> None:
    """The silent 404 this page has already shipped twice.

    `{{ 'docs/assets/ava-tour.mp4' | url }}` is not a markdown link, so
    `mkdocs build --strict` never looks at it. sync.py's ASSETS is an explicit
    allow-list whose misses only WARN. And CI checks out tracked files only,
    while sync.py copies the working tree - so an untracked binary previews
    perfectly on the author's machine and 404s in production.

    Stack those three and you get the actual history: a2148e7 stripped the tour
    binaries and re-added only the svg/png ones, leaving a player whose source
    AND poster both 404'd on a green build, for weeks. Every layer stayed green
    because no layer owned the question. This test owns it.
    """
    require_git()
    home = _home()
    refs = sorted({r for r in _URL_REF.findall(home) if not r.endswith("/")})
    assert refs, "the landing template names no staged file - did the media go?"

    assets = _sync_value("ASSETS")            # source path -> staged destination
    staged = {dest: src for src, dest in assets.items()}
    unstaged = sorted(r for r in refs if r not in staged)
    assert not unstaged, (
        f"home.html points at files sync.py does not stage: {unstaged}\n"
        f"Add them to ASSETS in {SYNC.relative_to(ROOT)}, or drop the markup. A "
        "miss here only prints a warning, so the page ships a 404 on a green build."
    )

    tracked_assets = set(tracked())
    for ref in refs:
        src = staged[ref]
        assert (ROOT / src).is_file(), (
            f"home.html shows {ref} and its source {src} does not exist.\n"
            f"The masters are rendered by demo/src/tour-hero.ts - see demo/SCRIPT.md."
        )
        assert src in tracked_assets, (
            f"{src} is staged and referenced by home.html but is NOT tracked.\n"
            "sync.py copies the working tree and CI checks out tracked files only, "
            "so this previews locally and 404s in production. `git add` it."
        )


def test_landing_carries_exactly_one_video() -> None:
    """One walkthrough, and the landing page is the only place it plays.

    demo/out/ holds a dozen renders - six task tours, seven diagram clips, a
    chaptered cut - and any of them can be argued onto this page. The moment
    there are two, the visitor's first decision is which video is THE video,
    which is a decision about the website rather than about Ava. The task tours
    have a home already: the Get-started pages that teach the task.

    Nothing here judges whether the one video is any good. That is
    demo/SCRIPT.md's job and a human's.
    """
    count = len(_VIDEO_EL.findall(_home()))
    assert count == 1, (
        f"the landing page carries {count} <video> elements, expected exactly 1.\n"
        "If a second tour really belongs on the site, put it on the page that "
        "teaches its task and link it - see the 'Other videos' table in "
        "demo/SCRIPT.md."
    )


def test_landing_ships_no_img_svg_and_no_untracked_script() -> None:
    """Two ways to break this page that leave the build green.

    An <img src="*.svg"> inside .ava-landing is hijacked by
    javascripts/diagram-zoom.js:142 into a fullscreen lightbox, silently turning
    the centrepiece into a diagram viewer. And a script listed in
    extra_javascript that is untracked previews locally and 404s in CI, because
    sync.py copies the working tree while CI checks out only tracked files -
    which is the carousel.js failure recorded in javascripts/chrome.js.
    """
    require_git()
    assert not _IMG_SVG.search(_home()), (
        "home.html delivers an SVG as <img>. diagram-zoom.js will hijack its "
        "click into a lightbox. Inline the <svg> instead."
    )

    listed = re.findall(r"^\s*-\s*(javascripts/[\w.-]+\.js)", MKDOCS.read_text(encoding="utf-8"), re.M)
    assert listed, "mkdocs.yml lists no landing scripts - did extra_javascript move?"
    tracked_js = set(tracked("docs-site/javascripts/"))
    for rel in listed:
        path = f"docs-site/{rel}"
        assert (SITE / rel).is_file(), (
            f"mkdocs.yml lists {rel} and it does not exist. MkDocs does not check "
            "this: it would ship a <script src> that 404s, on a green build."
        )
        assert path in tracked_js, (
            f"{path} is listed in extra_javascript but is NOT tracked. sync.py "
            "copies the working tree, so it works in preview and 404s in production."
        )
