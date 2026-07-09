#!/usr/bin/env python3
"""Stage the public docs into docs-site/docs/ for MkDocs.

Copies a CURATED allow-list of source markdown + assets into the staging tree
(so nothing un-listed can ever leak onto the published site — the site is built
only from what this script copies). Relative paths are preserved so inter-doc
links keep resolving; any link that points at repo SOURCE (code, config, a
gitignored file, LICENSE/NOTICE, a non-published dir) is rewritten to a GitHub
blob/tree URL.

Run:  python docs-site/sync.py   (then `mkdocs build --strict -f docs-site/mkdocs.yml`)
The base repo URL is overridable for forks: AVA_DOCS_REPO_BASE=https://github.com/you/ava
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = HERE / "docs"

REPO_BASE = os.environ.get("AVA_DOCS_REPO_BASE", "https://github.com/JayMTea/Ava").rstrip("/")
BRANCH = os.environ.get("AVA_DOCS_BRANCH", "master")

# src (repo-relative) -> dst (staging-relative). READMEs that are linked as bare
# directories are staged as index.md so those directory links resolve.
# The site homepage (index.md) is NOT the README: it's the landing page,
# authored at docs-site/home.md (see HOME_PAGE below); the README becomes the
# "Why Ava?" overview page.
CURATED: dict[str, str] = {
    "README.md": "overview.md",
    "CONTRIBUTING.md": "CONTRIBUTING.md",
    "SECURITY.md": "SECURITY.md",
    "CHANGELOG.md": "CHANGELOG.md",
    "deploy/README.md": "deploy/README.md",
    "docs/AGENT_RUNTIME.md": "docs/AGENT_RUNTIME.md",
    "docs/CONNECT_YOUR_APPS.md": "docs/CONNECT_YOUR_APPS.md",
    "docs/CONNECT_YOUR_HARDWARE.md": "docs/CONNECT_YOUR_HARDWARE.md",
    "docs/MEMORY.md": "docs/MEMORY.md",
    "docs/MOBILE.md": "docs/MOBILE.md",
    "docs/CONNECTOR_SDK.md": "docs/CONNECTOR_SDK.md",
    "docs/DEVICE_CONNECTORS.md": "docs/DEVICE_CONNECTORS.md",
    "docs/RELEASING.md": "docs/RELEASING.md",
    "docs/PACKAGING_PLAN.md": "docs/PACKAGING_PLAN.md",
    "docs/WARM_AGENT_MODE.md": "docs/WARM_AGENT_MODE.md",
    "docs/HWINFO_VALIDATION.md": "docs/HWINFO_VALIDATION.md",
    "agent/docs/README.md": "agent/docs/README.md",
    "agent/docs/adr/README.md": "agent/docs/adr/index.md",
    "agent/docs/adr/0000-template.md": "agent/docs/adr/0000-template.md",
    "agent/docs/adr/0001-ssot-architecture-pipeline.md": "agent/docs/adr/0001-ssot-architecture-pipeline.md",
    "agent/docs/adr/0002-two-app-split.md": "agent/docs/adr/0002-two-app-split.md",
    "agent/docs/adr/0003-per-tool-egress-policies.md": "agent/docs/adr/0003-per-tool-egress-policies.md",
    "agent/docs/adr/0004-tala-layout-engine.md": "agent/docs/adr/0004-tala-layout-engine.md",
}

# The landing page lives in docs-site/ (site-specific, not repo docs). Its
# links are authored repo-relative and staged at the root, so the standard
# rewrite (as if the page were index.md) applies unchanged.
HOME_PAGE = ("home.md", "index.md")  # (docs-site-relative src, staging dst)

ASSETS: dict[str, str] = {
    "docs/assets/architecture.svg": "docs/assets/architecture.svg",
    "docs/assets/agent-remote-runtime.svg": "docs/assets/agent-remote-runtime.svg",
    "docs/assets/vitals-dashboard.png": "docs/assets/vitals-dashboard.png",
    # Narrated walkthrough video + its poster for the landing page.
    "docs/assets/reel-poster.png": "docs/assets/reel-poster.png",
    # Connect-your-apps walkthrough (docs/CONNECT_YOUR_APPS.md).
    "docs/assets/connect-app-tour.mp4": "docs/assets/connect-app-tour.mp4",
    "docs/assets/connect-app-1-connectors.png": "docs/assets/connect-app-1-connectors.png",
    "docs/assets/connect-app-2-detected.png": "docs/assets/connect-app-2-detected.png",
    "docs/assets/connect-app-3-connected.png": "docs/assets/connect-app-3-connected.png",
    "docs/assets/connect-device-tour.mp4": "docs/assets/connect-device-tour.mp4",
    "docs/assets/connect-hardware-tour.mp4": "docs/assets/connect-hardware-tour.mp4",
    "docs/assets/agent-setup-tour.mp4": "docs/assets/agent-setup-tour.mp4",
    "docs/assets/pwa-install-ios.png": "docs/assets/pwa-install-ios.png",
    "docs/assets/pwa-install-android.png": "docs/assets/pwa-install-android.png",
    "docs/assets/ava-tour.mp4": "docs/assets/ava-tour.mp4",
    "agent/docs/diagrams/system.svg": "agent/docs/diagrams/system.svg",
    "agent/docs/diagrams/network.svg": "agent/docs/diagrams/network.svg",
    "agent/docs/diagrams/security.svg": "agent/docs/diagrams/security.svg",
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


def _rewrite_target(target: str, src: str, src_dst: str) -> str:
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
    # Otherwise it points at repo source not on the site -> GitHub.
    abspath = REPO / resolved_noslash
    kind = "tree" if abspath.is_dir() or path_part.endswith("/") else "blob"
    url = f"{REPO_BASE}/{kind}/{BRANCH}/{resolved_noslash}"
    return url + (("#" + anchor) if anchor else "")


def _rewrite_links(text: str, src: str, src_dst: str) -> str:
    def repl(m: re.Match) -> str:
        return m.group(1) + _rewrite_target(m.group(2), src, src_dst) + m.group(3)
    text = _LINK.sub(repl, text)
    # GitHub renders markdown inside plain HTML blocks; MkDocs needs the
    # md_in_html opt-in attribute or the div's contents show as raw text.
    return text.replace('<div align="center">', '<div align="center" markdown>')


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
    if missing:
        print("WARNING: missing sources:\n  " + "\n  ".join(missing))
    print(f"staged {len(CURATED)} pages + {len(ASSETS)} assets -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
