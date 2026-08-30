"""An image a published page shows must actually ship with it.

MEASURED, not assumed. `mkdocs build --strict` fails on a broken internal LINK,
so it is easy to believe it covers images too. It does not: staging a page whose
`![...](../assets/x.png)` was absent from sync.py's ASSETS allow-list built clean
and exited 0. The page shipped with a broken image icon where the figure goes.

Three layers each decline to own the question, which is why it stays green:

  - sync.py's ASSETS is an explicit allow-list, and a miss only WARNS. main()
    appends to `missing`, prints, and returns 0.
  - mkdocs --strict does not validate image sources at all (above).
  - CI checks out TRACKED files, while sync.py copies the WORKING TREE. So an
    untracked capture previews perfectly on the machine that made it and 404s
    in production, where nothing looks again.

This is the same failure the landing page shipped twice with its <video> - see
tests/test_landing_page.py, which guards the Jinja template's `| url` refs. This
module guards the markdown half: every image reference on every published page.

What it deliberately does NOT claim: that any image is CURRENT, or that it shows
what the prose beside it says. A stale capture of a redesigned panel is a real
problem and no static scan can see it. That judgement stays with a human reading
a diff.

Run: python -m pytest tests/test_docs_assets.py -q
"""
import ast
import pathlib
import posixpath
import re

from gitfiles import require_git, tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYNC = ROOT / "docs-site" / "sync.py"

# Markdown images only: `![alt](target)`. Links are mkdocs --strict's job and it
# actually does that one. The alt text may contain brackets (several of these
# alts quote UI copy), so the class excludes only an unescaped closing bracket
# at the top level, matching sync.py's own _LINK pattern.
_IMG = re.compile(r"!\[(?:[^\[\]]|\[[^\]]*\])*\]\(([^)\s]+)")
_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


def _sync_dict(name: str) -> dict[str, str]:
    """Read a dict literal out of sync.py without importing it."""
    tree = ast.parse(SYNC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets = getattr(node, "targets", []) or ([node.target] if getattr(node, "target", None) else [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {SYNC.relative_to(ROOT)}")


def test_every_published_image_is_staged_and_tracked() -> None:
    """The check the three layers above each leave to someone else."""
    require_git()
    pages = _sync_dict("CURATED")
    assets = _sync_dict("ASSETS")
    tracked_files = set(tracked())

    unstaged: list[str] = []
    untracked: list[str] = []
    missing: list[str] = []

    for src in pages:
        page = ROOT / src
        if not page.is_file():
            continue
        for target in _IMG.findall(page.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "data:", "#", "/")):
                continue
            if not target.lower().endswith(_EXT):
                continue
            # Resolve against the PAGE's directory, exactly as a reader's
            # browser and sync.py's _rewrite_target both do.
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(src), target))
            where = f"{src} -> {target}"
            if resolved not in assets:
                unstaged.append(where)
            elif resolved not in tracked_files:
                untracked.append(where)
            elif not (ROOT / resolved).is_file():
                missing.append(where)

    assert not unstaged, (
        "these published pages show images that sync.py does not stage:\n  "
        + "\n  ".join(unstaged)
        + f"\nAdd them to ASSETS in {SYNC.relative_to(ROOT)}. A miss there only "
        "prints a warning and mkdocs --strict does not check image sources, so "
        "the page ships a broken image on a green build."
    )
    assert not untracked, (
        "these images are staged and referenced but NOT tracked by git:\n  "
        + "\n  ".join(untracked)
        + "\nsync.py copies the working tree and CI checks out tracked files "
        "only, so they preview locally and 404 in production. `git add` them."
    )
    assert not missing, (
        "these images are staged and tracked but absent from the working "
        "tree:\n  " + "\n  ".join(missing)
    )


def test_every_staged_asset_exists() -> None:
    """The allow-list's own half: an entry naming a file that is not there.

    sync.py only warns on this, so an asset deleted by a cleanup keeps its
    ASSETS entry and every page pointing at it breaks together.
    """
    require_git()
    gone = sorted(src for src in _sync_dict("ASSETS") if not (ROOT / src).is_file())
    assert not gone, (
        f"ASSETS in {SYNC.relative_to(ROOT)} names files that do not exist: {gone}\n"
        "Either restore them or drop the entries, together with whatever "
        "references them."
    )
