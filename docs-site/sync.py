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
    "README.md": "overview.md",
    "CONTRIBUTING.md": "CONTRIBUTING.md",
    "TRADEMARK.md": "TRADEMARK.md",
    "SECURITY.md": "SECURITY.md",
    "CHANGELOG.md": "CHANGELOG.md",
    "deploy/README.md": "deploy/README.md",
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
    "docs/assets/architecture.svg": "docs/assets/architecture.svg",
    "docs/assets/agent-remote-runtime.svg": "docs/assets/agent-remote-runtime.svg",
    "docs/assets/vitals-dashboard.png": "docs/assets/vitals-dashboard.png",
    # The chat surface. docs/capabilities/chat.md ran 258 lines with no picture
    # of the thing it describes; this is that page's one screenshot.
    "docs/assets/chat.png": "docs/assets/chat.png",
    # Narrated walkthrough video + its poster for the landing page.
    "docs/assets/reel-poster.png": "docs/assets/reel-poster.png",
    # Connect-your-apps walkthrough (docs/CONNECT_YOUR_APPS.md).
    "docs/assets/connect-app-tour.mp4": "docs/assets/connect-app-tour.mp4",
    # The caption sidecar the <track> element points at. ASSETS is an explicit
    # allow-list, so an unlisted .vtt does not fail the build — it 404s at the
    # viewer, and a caption track that 404s looks exactly like a video with no
    # captions, which is the failure nobody reports.
    "docs/assets/connect-app-tour.vtt": "docs/assets/connect-app-tour.vtt",
    "docs/assets/ava-tour.vtt": "docs/assets/ava-tour.vtt",
    "docs/assets/choose-model-tour.vtt": "docs/assets/choose-model-tour.vtt",
    "docs/assets/connect-device-tour.vtt": "docs/assets/connect-device-tour.vtt",
    "docs/assets/agent-setup-tour.vtt": "docs/assets/agent-setup-tour.vtt",
    "docs/assets/install-tour.vtt": "docs/assets/install-tour.vtt",
    "docs/assets/connect-app-1-connectors.png": "docs/assets/connect-app-1-connectors.png",
    "docs/assets/connect-app-2-detected.png": "docs/assets/connect-app-2-detected.png",
    "docs/assets/connect-app-3-connected.png": "docs/assets/connect-app-3-connected.png",
    # The walkthrough gained two beats — the app embedded in the rail, and a question
    # answered from its own data. ASSETS is an explicit allow-list, so a new image
    # referenced from a page is a 404 on the site until it is named here.
    "docs/assets/connect-app-4-embedded.png": "docs/assets/connect-app-4-embedded.png",
    "docs/assets/connect-app-5-asked.png": "docs/assets/connect-app-5-asked.png",
    "docs/assets/connect-device-tour.mp4": "docs/assets/connect-device-tour.mp4",
    "docs/assets/choose-model-tour.mp4": "docs/assets/choose-model-tour.mp4",
    "docs/assets/install-tour.mp4": "docs/assets/install-tour.mp4",
    "docs/assets/choose-model-1-hardware.png": "docs/assets/choose-model-1-hardware.png",
    "docs/assets/choose-model-2-test.png": "docs/assets/choose-model-2-test.png",
    "docs/assets/choose-model-3-brain.png": "docs/assets/choose-model-3-brain.png",
    "docs/assets/choose-model-4-store.png": "docs/assets/choose-model-4-store.png",
    "docs/assets/agent-setup-1-status.png": "docs/assets/agent-setup-1-status.png",
    "docs/assets/agent-setup-2-provision.png": "docs/assets/agent-setup-2-provision.png",
    "docs/assets/install-1-terminal.png": "docs/assets/install-1-terminal.png",
    # docs/assets/hardware-detected.png is deliberately NOT staged: it is a crop
    # of the old row-layout "Your hardware" panel, no page references it, and the
    # current hero layout is already shown by choose-model-1-hardware.png.
    "docs/assets/agent-setup-tour.mp4": "docs/assets/agent-setup-tour.mp4",
    "docs/assets/pwa-install-ios.png": "docs/assets/pwa-install-ios.png",
    "docs/assets/pwa-install-android.png": "docs/assets/pwa-install-android.png",
    "docs/assets/ava-tour.mp4": "docs/assets/ava-tour.mp4",
    "agent/docs/diagrams/security.svg": "agent/docs/diagrams/security.svg",
    # og:image / twitter:image for every page (docs-site/overrides/main.html).
    # 1200x630, captured from the real app by demo/manifests/og-card.yaml.
    "docs/assets/og-card.png": "docs/assets/og-card.png",
    # Staged as assets, not pages: both are fixed-width plain text that markdown
    # would reflow into mush. Copying them verbatim keeps the licence readable
    # AND makes README's `[Apache-2.0](LICENSE)` / `[NOTICE](NOTICE)` links
    # resolve on the site instead of being stripped to bare labels.
    "LICENSE": "LICENSE.txt",
    "NOTICE": "NOTICE.txt",
}

# Where every curated repo path lands in the staging tree, so links can be
# recomputed as correct relative paths.
_SRC_TO_DST: dict[str, str] = {**CURATED, **ASSETS}
# Bare-directory links (e.g. `examples/hello-app/`) resolve to that dir's README page.
_DIR_TO_INDEX = {os.path.dirname(s): d for s, d in CURATED.items()
                 if os.path.basename(s) == "README.md" and os.path.dirname(s)}

# The label may itself contain one nested [...] (e.g. a badge/image link
# `[![alt](img)](target)`), so allow a single level of bracket nesting.
_LINK = re.compile(r"(!?\[(?:[^\[\]]|\[[^\]]*\])*\]\()([^)]+)(\))")


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

    def repl(m: re.Match) -> str:
        target = _rewrite_target(m.group(2), src, src_dst)
        if target is None:                 # unlinkable source path: keep the label
            return m.group(1).removeprefix("!")[1:-2]
        return m.group(1) + target + m.group(3)
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
        dp.write_text(text, encoding="utf-8")
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
