#!/usr/bin/env python3
"""Stage the public docs into docs-site/docs/ for MkDocs.

Copies a CURATED allow-list of source markdown + assets into the staging tree
(so nothing un-listed can ever leak onto the published site — the site is built
only from what this script copies). Relative paths are preserved so inter-doc
links keep resolving; any link that points at repo SOURCE (code, config, a
gitignored file, LICENSE/NOTICE, a non-published dir) is rewritten to a GitHub
blob/tree URL — but only when the source repo is public (see REPO_BASE).

Run:  python docs-site/sync.py   (then `mkdocs build --strict -f docs-site/mkdocs.yml`)
Set AVA_DOCS_REPO_BASE=https://github.com/you/ava to link source (public repos).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = HERE / "docs"

# Links into repo SOURCE become GitHub URLs only if that repo is public. Unset
# (the default) means it isn't, and minting the URLs anyway would publish 404s —
# so those links are dropped and just their label is kept.
REPO_BASE = os.environ.get("AVA_DOCS_REPO_BASE", "").rstrip("/")
BRANCH = os.environ.get("AVA_DOCS_BRANCH", "master")

# src (repo-relative) -> dst (staging-relative). READMEs that are linked as bare
# directories are staged as index.md so those directory links resolve.
# The site homepage (index.md) is NOT the README: it's the landing page,
# authored at docs-site/home.md (see HOME_PAGE below); the README becomes the
# "Why Ava?" overview page.
CURATED: dict[str, str] = {
    # The site's "Why Ava?" page and the GitHub README are NOT the same document.
    # README.md serves someone standing in the repo (badges, licence, citation,
    # a quickstart they can paste); the site page serves someone deciding whether
    # Ava is for them, and answered that at word 988 while the reader was still
    # scrolling. docs/WHY_AVA.md is the site's copy: same claims, reader order.
    "docs/WHY_AVA.md": "overview.md",
    "CONTRIBUTING.md": "CONTRIBUTING.md",
    "TRADEMARK.md": "TRADEMARK.md",
    "SECURITY.md": "SECURITY.md",
    "CHANGELOG.md": "CHANGELOG.md",
    "deploy/README.md": "deploy/README.md",
    # The install page's reference half. deploy/README.md ran 3,476 words, of
    # which a first-time installer needs about 400: the rest is manual profile
    # alternatives, Windows shell diagnostics, the env-var table and cosign
    # verification. Splitting them lets the happy path stay a happy path
    # without deleting a line of what an operator eventually needs.
    "docs/INSTALL_REFERENCE.md": "docs/INSTALL_REFERENCE.md",
    # Same split, one page down the funnel: "Set up the agent" is a two-click
    # GUI step, and the runtime-authoring material (writing an AgentRuntime,
    # pinning versions, networking) belongs beside it, not inside it.
    "docs/AGENT_RUNTIME_REFERENCE.md": "docs/AGENT_RUNTIME_REFERENCE.md",
    # Capabilities section (nav order: overview first, then one page per area).
    "docs/capabilities/index.md": "docs/capabilities/index.md",
    "docs/capabilities/chat.md": "docs/capabilities/chat.md",
    "docs/capabilities/vitals.md": "docs/capabilities/vitals.md",
    "docs/capabilities/operations.md": "docs/capabilities/operations.md",
    "docs/capabilities/data.md": "docs/capabilities/data.md",
    "docs/capabilities/connectors.md": "docs/capabilities/connectors.md",
    "docs/capabilities/agent.md": "docs/capabilities/agent.md",
    "docs/AGENT_RUNTIME.md": "docs/AGENT_RUNTIME.md",
    "docs/ALLOCATION.md": "docs/ALLOCATION.md",
    "docs/CONNECT_YOUR_APPS.md": "docs/CONNECT_YOUR_APPS.md",
    "docs/CHOOSE_A_MODEL.md": "docs/CHOOSE_A_MODEL.md",
    "docs/MEMORY.md": "docs/MEMORY.md",
    "docs/PERSONA.md": "docs/PERSONA.md",
    "docs/BRANDING.md": "docs/BRANDING.md",
    "docs/EVIDENCE.md": "docs/EVIDENCE.md",
    "docs/BIOMETRICS.md": "docs/BIOMETRICS.md",
    "docs/MOBILE.md": "docs/MOBILE.md",
    "docs/CONNECTOR_SDK.md": "docs/CONNECTOR_SDK.md",
    "docs/DEVICE_CONNECTORS.md": "docs/DEVICE_CONNECTORS.md",
    "docs/CONNECT_HOME_ASSISTANT.md": "docs/CONNECT_HOME_ASSISTANT.md",
    "docs/RELEASING.md": "docs/RELEASING.md",
    "docs/HWINFO_VALIDATION.md": "docs/HWINFO_VALIDATION.md",
    "agent/docs/README.md": "agent/docs/README.md",
    "agent/docs/adr/README.md": "agent/docs/adr/index.md",
    "agent/docs/adr/0000-template.md": "agent/docs/adr/0000-template.md",
    "agent/docs/adr/0001-ssot-architecture-pipeline.md": "agent/docs/adr/0001-ssot-architecture-pipeline.md",
    "agent/docs/adr/0002-two-app-split.md": "agent/docs/adr/0002-two-app-split.md",
    "agent/docs/adr/0003-per-tool-egress-policies.md": "agent/docs/adr/0003-per-tool-egress-policies.md",
    "agent/docs/adr/0004-tala-layout-engine.md": "agent/docs/adr/0004-tala-layout-engine.md",
    "agent/docs/adr/0005-model-load-allocation.md": "agent/docs/adr/0005-model-load-allocation.md",
}

# The landing page lives in docs-site/ (site-specific, not repo docs). Its
# links are authored repo-relative and staged at the root, so the standard
# rewrite (as if the page were index.md) applies unchanged.
HOME_PAGE = ("home.md", "index.md")  # (docs-site-relative src, staging dst)

ASSETS: dict[str, str] = {
    # Brand wordmark for the header and the landing hero. Traced from the
    # transparent brand master in frontend/public/assets/icons/ and filled with
    # `currentColor` so ONE file inks itself per scheme (extra.css masks it) —
    # the master's navy is invisible on the dark canvas. See the file's comment.
    "docs/assets/ava-wordmark.svg": "docs/assets/ava-wordmark.svg",
    # The landing hero's stacked lockup (mark over wordmark), as a PAIR. Unlike
    # the wordmark above it cannot be masked: the mark carries the brand
    # gradient, and a mask throws colour away and keeps only the silhouette. So
    # the scheme is served by swapping the file, not by inking one — `-ink` is
    # the light scheme (near-black wordmark) and `-white` is the dark one, and
    # both keep the mark blue. SVG rather than the 1200w PNG beside it in the
    # export set: 2 KB, resolution-independent, and the letterforms stay sharp
    # at any hero width. Exported into frontend/public/assets/others/, which is
    # NOT where they can be served from — public/ is copied verbatim into the
    # tracked dist and precached by workbox, so the whole variant set would
    # ship to every app client to render one logo on a docs page.
    "docs/assets/ava-stacked-gradient-ink.svg": "docs/assets/ava-stacked-gradient-ink.svg",
    "docs/assets/ava-stacked-gradient-white.svg": "docs/assets/ava-stacked-gradient-white.svg",
    # The site's tab icon, INHERITED from the app rather than redrawn — same rule
    # as the theme icons below. Without it Material serves its own default
    # favicon, so the docs advertised someone else's mark while the app carried
    # Ava's. It is the app's own file, so `python tools/sync_icons.py` keeps this
    # in step with the PWA icons and tests/test_icon_sync.py fails if it drifts.
    # No mask needed here (unlike the wordmark): a browser tab is neutral chrome,
    # not the accent-painted header, and the mark was checked at 16px on both a
    # dark and a light strip.
    "frontend/public/favicon.svg": "docs/assets/favicon.svg",
    "docs/assets/architecture.svg": "docs/assets/architecture.svg",
    "docs/assets/agent-remote-runtime.svg": "docs/assets/agent-remote-runtime.svg",
    # "What leaves your machine" — the owner-facing privacy picture, on the
    # Why Ava? page, docs/capabilities/data.md and SECURITY.md. The privacy
    # claim was argued in 454 words of prose on three pages and drawn nowhere.
    "docs/assets/egress.svg": "docs/assets/egress.svg",
    # Detail CROPS, from demo/manifests/docs-crops.yaml. The docs column caps
    # media at 44rem, so a 1920px full-page capture lands at ~704px and small UI
    # becomes unreadable: each of these is the one element a page was arguing
    # about in prose while showing it at 0.37x, or not at all.
    "docs/assets/chat-tools-used.png": "docs/assets/chat-tools-used.png",
    "docs/assets/data-secrets.png": "docs/assets/data-secrets.png",
    "docs/assets/approvals-banner.png": "docs/assets/approvals-banner.png",
    # ASSETS is an explicit allow-list, so a new image referenced from a page is
    # a 404 on the site until it is named here.
    "docs/assets/install-1-terminal.png": "docs/assets/install-1-terminal.png",
    # docs/assets/hardware-detected.png is deliberately NOT staged: it is a crop
    # of the old row-layout "Your hardware" panel and no page references it.
    "docs/assets/pwa-install-ios.png": "docs/assets/pwa-install-ios.png",
    "docs/assets/pwa-install-android.png": "docs/assets/pwa-install-android.png",
    # The landing hero's narrated walkthrough, its poster, and its captions.
    # Rendered by demo/src/tour-hero.ts from demo/vo-hero/SCRIPT.md.
    #
    # A MISSING ASSET HERE IS SILENT. main() only appends to `missing` and
    # prints a warning; mkdocs --strict checks markdown links, not the `src` of
    # a <video> in a Jinja template. That is how the whole tour vanished once
    # already: a2148e7 stripped the binaries from history to shrink the repo and
    # re-added only the svg/png ones, so the landing page shipped a player whose
    # source AND poster both 404'd, and every build stayed green. If you strip
    # these again, delete the embed in overrides/home.html in the same commit.
    "docs/assets/reel-poster.png": "docs/assets/reel-poster.png",
    "docs/assets/ava-tour.mp4": "docs/assets/ava-tour.mp4",
    # The .vtt is listed for the same reason as the mp4, and matters more: an
    # unlisted caption track does not fail anything, it just 404s and leaves a
    # video that looks like it has no captions at all.
    "docs/assets/ava-tour.vtt": "docs/assets/ava-tour.vtt",
    "agent/docs/diagrams/security.svg": "agent/docs/diagrams/security.svg",
    # Staged as assets, not pages: both are fixed-width plain text that markdown
    # would reflow into mush. Copying them verbatim keeps the licence readable
    # AND makes README's `[Apache-2.0](LICENSE)` / `[NOTICE](NOTICE)` links
    # resolve on the site instead of being stripped to bare labels.
    "LICENSE": "LICENSE.txt",
    "NOTICE": "NOTICE.txt",
}

# Per-page MkDocs front matter, keyed by STAGED destination and prepended on the
# way out. It lives here rather than in the source file because these keys are
# instructions to one theme: a repo doc is read on GitHub too, where a `hide:`
# block renders as a stray table above the first heading. Staging is the only
# place that knows a page is being published, so it is where the theme's
# vocabulary belongs.
FRONT_MATTER: dict[str, str] = {
    # `Why Ava?` is the one top-level nav entry that is a PAGE, not a section,
    # so navigation.tabs gives it a tab and the sidebar then renders a tree of
    # exactly one item: a link to the page you are already reading, directly
    # under the tab that is already highlighted. With the H1 that made three
    # identical labels in three chrome regions before the prose started.
    #
    # Hiding it costs this page nothing. The tabs are still the route out,
    # which is the same bet home.md already makes with the same key, and no
    # page on this site has a right-hand TOC to fall back on either way —
    # extra.css kills .md-sidebar--secondary globally so the content reflows
    # wider. So the page simply has no sidebar now, and the prose gets the
    # column. Only add this key for a section of one; a page that shares a tab
    # with siblings needs the tree to reach them.
    "overview.md": "---\nhide:\n  - navigation\n---\n\n",
}

# Where every curated repo path lands in the staging tree, so links can be
# recomputed as correct relative paths.
_SRC_TO_DST: dict[str, str] = {**CURATED, **ASSETS}
# Bare-directory links (e.g. `examples/device-app/`) resolve to that dir's README page.
_DIR_TO_INDEX = {os.path.dirname(s): d for s, d in CURATED.items()
                 if os.path.basename(s) == "README.md" and os.path.dirname(s)}

# The label may itself contain one nested [...] (e.g. a badge/image link
# `[![alt](img)](target)`), so allow a single level of bracket nesting.
_LINK = re.compile(r"(!?\[(?:[^\[\]]|\[[^\]]*\])*\]\()([^)]+)(\))")

# ...but that leaves the NESTED image's own src sitting inside group(1), which
# _LINK copies through verbatim. For a badge that is right (the src is an
# off-site URL and _rewrite_target hands those back unchanged) and for a page
# staged at its authored depth it is invisible (the rewrite is a no-op). It is
# wrong for a local image on a page that MOVES: docs/WHY_AVA.md is staged to
# overview.md at the site root, so `[![alt](assets/egress.svg)](assets/egress.svg)`
# had its link rewritten to docs/assets/egress.svg and its <img> left pointing at
# assets/egress.svg, which does not exist at the root. `mkdocs build --strict`
# caught it, but only because the target was missing; a stale-but-existing path
# would have shipped a silently wrong image. Rewrite the inner src too.
_NESTED_IMG = re.compile(r"(!\[[^\[\]]*\]\()([^)]+)(\))")


def _staged_dst(resolved_noslash: str) -> str | None:
    if resolved_noslash in _SRC_TO_DST:
        return _SRC_TO_DST[resolved_noslash]
    if resolved_noslash in _DIR_TO_INDEX:
        return _DIR_TO_INDEX[resolved_noslash]
    return None


def _rewrite_target(target: str, src: str, src_dst: str) -> str | None:
    raw = target.strip()
    if raw.startswith(("http://", "https://", "#", "mailto:")):
        return target
    path_part, _, anchor = raw.partition("#")
    if not path_part:                      # pure anchor
        return target
    src_dir = os.path.dirname(src)         # repo-relative dir the link was AUTHORED in
    dst_dir = os.path.dirname(src_dst)     # staging-relative dir the page LANDS in
    # Resolve the link against the SOURCE layout, then re-relativize against the
    # staged location (pages can move during staging, e.g. README.md remaps).
    resolved = os.path.normpath(os.path.join(src_dir, path_part))
    resolved_noslash = resolved.rstrip("/").lstrip("./")

    dst = _staged_dst(resolved_noslash)
    if dst is not None:                    # an internal page/asset/index dir
        rel = os.path.relpath(dst, start=dst_dir or ".")
        return rel + (("#" + anchor) if anchor else "")
    # Otherwise it points at repo source not on the site -> GitHub, if it's public.
    if not REPO_BASE:
        return None                        # nothing public to point at
    abspath = REPO / resolved_noslash
    kind = "tree" if abspath.is_dir() or path_part.endswith("/") else "blob"
    url = f"{REPO_BASE}/{kind}/{BRANCH}/{resolved_noslash}"
    return url + (("#" + anchor) if anchor else "")


# Badge images hosted off-site. On GitHub these are free — the reader is already
# talking to github.com. On the published site they are not: every visitor to the
# page would fetch them from a third party, handing it their IP and the page they
# are on. This site's entire claim is that nothing leaves your machine unless you
# send it (it is why mkdocs.yml sets `font: false` and why overrides/partials/
# source.html exists), and a row of shields.io badges quietly contradicts it on
# the very page that makes the argument.
#
# Only the SITE copy is stripped. README.md keeps its badges for GitHub, which is
# where badges belong and where they cost nothing.
_BADGE_LINE = re.compile(
    r"^[ \t]*(?:\[!\[[^\]]*\]\(https?://(?:img\.shields\.io|badgen\.net|badge\.fury\.io)/[^)]*\)\][^\n]*|"
    r"!\[[^\]]*\]\(https?://(?:img\.shields\.io|badgen\.net|badge\.fury\.io)/[^)]*\))[ \t]*$",
    re.M,
)


def _strip_offsite_badges(text: str) -> str:
    text = _BADGE_LINE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _rewrite_links(text: str, src: str, src_dst: str) -> str:
    text = _strip_offsite_badges(text)

    def img_repl(im: re.Match) -> str:
        # A nested image inside a link label. Unlinkable (off-site badge, or a
        # source path with no public URL) keeps its src: an <img> has no label to
        # fall back to, so dropping the target would leave a broken image where a
        # working one stood.
        inner = _rewrite_target(im.group(2), src, src_dst)
        return im.group(1) + (inner if inner is not None else im.group(2)) + im.group(3)

    def repl(m: re.Match) -> str:
        label = _NESTED_IMG.sub(img_repl, m.group(1))
        target = _rewrite_target(m.group(2), src, src_dst)
        if target is None:                 # unlinkable source path: keep the label
            return label.removeprefix("!")[1:-2]
        return label + target + m.group(3)
    text = _LINK.sub(repl, text)
    # GitHub renders markdown inside plain HTML blocks; MkDocs needs the
    # md_in_html opt-in attribute or the div's contents show as raw text.
    return text.replace('<div align="center">', '<div align="center" markdown>')


# Glyphs the docs reference as `:ava-<name>:`. Same rule as the palette: the
# icons are INHERITED from the app (frontend/src/lib/icons.tsx is the SSOT), not
# redrawn here, so the site can never drift from the product's own icon set — and
# so no page has to fall back to an emoji, which renders as a different picture
# on every OS and carries no accessible name.
ICON_SRC = REPO / "frontend/src/lib/icons.tsx"
ICONS_WANTED = ("check", "close")
# Material resolves additional icons from `<custom_dir>/.icons/<ns>/<name>.svg`,
# NOT from docs_dir — so these are written beside the templates, not into OUT.
ICONS_OUT = HERE / "overrides" / ".icons" / "ava"
_ICON_RE = r"^\s{2}%s:\s*\n?\s*'(<svg.*?</svg>)'"


def _stage_icons() -> list[str]:
    """Copy the app's own SVGs into the theme's icon namespace. Returns misses."""
    if not ICON_SRC.is_file():
        return [str(ICON_SRC.relative_to(REPO))]
    src = ICON_SRC.read_text(encoding="utf-8")
    ICONS_OUT.mkdir(parents=True, exist_ok=True)
    missed = []
    for name in ICONS_WANTED:
        m = re.search(_ICON_RE % re.escape(name), src, re.S | re.M)
        if not m:
            missed.append(f"{ICON_SRC.name}:{name} (icon not found)")
            continue
        # stroke="currentColor" already, so the glyph inherits the surrounding
        # text colour in both light and dark schemes with no extra CSS.
        (ICONS_OUT / f"{name}.svg").write_text(m.group(1) + "\n", encoding="utf-8")
    return missed


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    missing = []
    for src, dst in CURATED.items():
        sp = REPO / src
        if not sp.is_file():
            missing.append(src)
            continue
        text = _rewrite_links(sp.read_text(encoding="utf-8"), src, dst)
        dp = OUT / dst
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_text(FRONT_MATTER.get(dst, "") + text, encoding="utf-8")
    home_src, home_dst = HOME_PAGE
    hp = HERE / home_src
    if hp.is_file():
        (OUT / home_dst).write_text(
            _rewrite_links(hp.read_text(encoding="utf-8"), home_dst, home_dst),
            encoding="utf-8",
        )
    else:
        missing.append(f"docs-site/{home_src}")
    for src, dst in ASSETS.items():
        sp = REPO / src
        if not sp.is_file():
            missing.append(src)
            continue
        dp = OUT / dst
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sp, dp)
    # Local site assets (theme CSS/JS) that live under docs-site/, not the repo.
    for local in ("stylesheets", "javascripts"):
        src_dir = HERE / local
        if src_dir.is_dir():
            shutil.copytree(src_dir, OUT / local)
    # The palette is INHERITED from the app, not copied: stage the app's design
    # tokens (SSOT) re-keyed from the app's theme switch (html[data-theme]) to
    # Material's scheme attribute (body[data-md-color-scheme]). The dark :root
    # block stays as-is (slate needs no override); the light block lands on the
    # body when Material is in its default scheme. stylesheets/extra.css maps
    # Material's --md-* variables onto these tokens.
    tokens = REPO / "frontend/src/styles/tokens.css"
    css = tokens.read_text(encoding="utf-8")
    css = css.replace(':root[data-theme="light"]', '[data-md-color-scheme="default"]')
    (OUT / "stylesheets").mkdir(parents=True, exist_ok=True)
    (OUT / "stylesheets" / "tokens.css").write_text(css, encoding="utf-8")
    missing += _stage_icons()
    if missing:
        print("WARNING: missing sources:\n  " + "\n  ".join(missing))
    if not REPO_BASE:
        print("note: AVA_DOCS_REPO_BASE unset — links into repo source are rendered "
              "as plain labels. The docs workflow sets it; export "
              "AVA_DOCS_REPO_BASE=https://github.com/JayMTea/Ava to preview as published.")
    print(f"staged {len(CURATED)} pages + {len(ASSETS)} assets -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
