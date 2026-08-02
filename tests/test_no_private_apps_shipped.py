"""A development-only app must not reach the repo, the docs, or the IMAGE.

`.gitignore` and `.git/info/exclude` keep a maintainer's own apps out of the
tracked tree, and `tests/test_no_owner_identity.py` keeps their NAMES out of
tracked prose. Neither one protects the published image, and that was the hole:

    ava_bridge/connectors.py:29   BUILTIN_DIR = <repo root>/connectors
    deploy/Dockerfile             COPY . /app

`COPY .` copies the WORKING TREE, not the index, and the build context was the
repo root with no `connectors/` entry in `.dockerignore`. On a box where
AVA_HOME is the repo root — the maintainer's, by default — `connectors/` is also
where that box's own private apps are installed at runtime. So every image built
there embedded them as first-party built-ins: loaded for every user of that
image, with no install step and no manifest edit. `install.sh` always passes
`--build`, so this was not hypothetical. Git protected the repo; nothing
protected the image.

Three apps did ship that way as tracked `examples/` folders before this guard
existed. They were the owner's demo material for videos and screenshots, never
products, and they are gone from the tree.

What this file checks, none of it requiring anyone to write a private name down:

1. No tracked file lives under a private app's directory.
2. No tracked doc or example names a private app.
3. `.dockerignore` still default-denies `connectors/` and still re-includes
   every shipped built-in — no more, no less.

"Private" is DERIVED, not listed: an app directory that is on disk but that git
does not track, and that is not the runtime install of a shipped example (those
are legitimate — `examples/<id>/`'s README tells you to make one). That way a
new private app is covered the moment it appears, and this file never has to
contain the names it is protecting.

The derivation only has teeth where the apps exist, so on a fork checks 1 and 2
are vacuous and only check 3 bites. That asymmetry is correct and matches
`test_no_owner_identity`: only the person holding a private app can leak it. The
durable, name-based half of this lives in `.git/info/private-names` (local-only,
read by that test); this file is the half that needs no bookkeeping.

Same style as tests/test_diagram_sync.py: a static scan over `git ls-files` with
each failure message written as the fix instruction.
"""
from __future__ import annotations

import pathlib
import re

import yaml
from gitfiles import ROOT, tracked

# Extensions worth reading. Binaries are skipped deliberately: docs/assets holds
# multi-megabyte PNGs and MP4s, and a name that appears only inside a rendered
# screenshot is a picture, not a string a grep or a code search will surface.
_TEXTUAL = {".md", ".py", ".yaml", ".yml", ".json", ".ts", ".tsx", ".js", ".mjs",
            ".sh", ".txt", ".css", ".html", ".toml", ".cfg", ".ini", ".env",
            ".example", ".template", ".tmpl", ".d2"}

# Directories that hold app folders: (path, whether a subdir may be a runtime
# install of a shipped example).
_APP_DIRS = ("connectors", "examples")

# Word-collision escape hatch. Empty on purpose. A derived app name is matched on
# word boundaries against tracked prose, so an app named for a common word ("mail",
# "notes") would fail this guard on documentation that has nothing to do with it.
# Add such a name here — with a comment saying which app it is NOT — rather than
# weakening the scan. Renaming the app is the better fix where it is possible.
_NAME_COLLISIONS: set[str] = set()


def _manifest_id(d: pathlib.Path) -> str:
    """The connector id `d` declares, or "" if it declares none we can read."""
    try:
        doc = yaml.safe_load((d / "connector.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return ""
    return str((doc or {}).get("id") or "") if isinstance(doc, dict) else ""


def _tracked_dirs(parent: str) -> set[str]:
    """Names of subdirectories of `parent` that contain at least one tracked file."""
    out = set()
    for rel in tracked(f"{parent}/"):
        parts = rel.split("/")
        if len(parts) > 2:              # parent/<name>/... — a file directly in
            out.add(parts[1])           # parent (README.md) names no app
    return out


def _shipped_ids() -> set[str]:
    """Ids a forker is TOLD to install, so their runtime installs are expected.

    `examples/hello-app`'s README says to copy it to `$AVA_HOME/connectors/hello`,
    which on this box is `connectors/hello` — an untracked directory that is not a
    private app. Matching on the declared id rather than the folder name is what
    makes that distinction, since the two deliberately differ.
    """
    ids = set()
    for name in _tracked_dirs("examples"):
        ids.add(name)
        if cid := _manifest_id(ROOT / "examples" / name):
            ids.add(cid)
    return ids


def _private_apps() -> dict[str, pathlib.Path]:
    """{name: path} for on-disk app dirs that are neither tracked nor a shipped
    example's runtime install."""
    shipped = _shipped_ids()
    found: dict[str, pathlib.Path] = {}
    for parent in _APP_DIRS:
        base = ROOT / parent
        if not base.is_dir():
            continue
        keep = _tracked_dirs(parent)
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            if d.name in keep or d.name in shipped or d.name in _NAME_COLLISIONS:
                continue
            if _manifest_id(d) in shipped and _manifest_id(d):
                continue
            found[d.name] = d
    return found


def test_no_tracked_file_lives_under_a_private_app() -> None:
    """The `examples/` half of the original leak: a private app tracked outright."""
    private = _private_apps()
    if not private:
        return                          # a fork, or a box with no private apps
    offenders = []
    for rel in tracked():
        parts = rel.split("/")
        if len(parts) > 2 and parts[0] in _APP_DIRS and parts[1] in private:
            offenders.append(rel)
    assert not offenders, (
        f"these tracked files belong to a private app {sorted(private)}: "
        f"{sorted(offenders)}. Run `git rm -r --cached <dir>` to untrack it, then "
        "add the directory to .git/info/exclude (local-only, so the name is not "
        "published) — never to .gitignore.")


def test_no_tracked_doc_or_example_names_a_private_app() -> None:
    """A name leaks as prose long after the folder stops shipping — a docs table
    row, a link to a folder that no longer exists, a screenshot's alt text.

    Matched as a plain substring, NOT as `\\b<name>\\b`. A word boundary needs a
    non-word character on both sides, and `_` is a word character — so the
    bounded pattern sailed past `<app>_token` in a screenshot's alt text, which
    is exactly the shape these names take once they appear as identifiers
    (`<app>_token`, `<app>.log_workout`, `<app>-api`). These names are
    distinctive enough that a substring hit is a real hit.
    """
    private = _private_apps()
    if not private:
        return
    patterns = {n: re.compile(re.escape(n), re.I) for n in private}
    offenders = []
    for rel in tracked():
        if rel.split("/")[0] not in (*_APP_DIRS, "docs"):
            continue
        if pathlib.Path(rel).suffix.lower() not in _TEXTUAL:
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pat in patterns.items():
            if pat.search(text):
                offenders.append(f"{rel}: names the private app {name!r}")
    assert not offenders, (
        "tracked files name an app that does not ship, so a reader gets a broken "
        "link and the name is published:\n  - " + "\n  - ".join(sorted(offenders)) +
        "\nRewrite the passage around a shipped example (examples/hello-app, "
        "examples/device-app, examples/home-assistant) so it still teaches what it "
        "taught. If the word is an unrelated collision, add it to _NAME_COLLISIONS "
        "in this file with a comment.")


def _dockerignore() -> list[str]:
    return [ln.strip() for ln in
            (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_dockerignore_default_denies_the_connectors_directory() -> None:
    """Without this line, `COPY . /app` bakes every app on the build box into the
    image as a built-in. This is the only check here that runs on a fork too."""
    assert any(ln in ("connectors/*", "connectors/**", "connectors/", "connectors")
               for ln in _dockerignore()), (
        "the `connectors/*` line is gone from .dockerignore, so deploy/Dockerfile's "
        "`COPY . /app` will bake every directory in connectors/ into the image as a "
        "first-party built-in (ava_bridge/connectors.py sets BUILTIN_DIR there) — "
        "including apps that are only gitignored. Restore the default-deny line and "
        "the `!connectors/<id>` allow-list beneath it.")


def test_the_dockerignore_allow_list_is_exactly_the_shipped_builtins() -> None:
    """Both directions matter. A re-include of something untracked ships a private
    app; a MISSING re-include silently drops a built-in Ava needs, which shows up
    as a dashboard with no health rows rather than as a build error."""
    reincluded = {ln[1:].removeprefix("connectors/").rstrip("/")
                  for ln in _dockerignore() if ln.startswith("!connectors/")}
    shipped = {rel.split("/")[1] for rel in tracked("connectors/")
               if len(rel.split("/")) > 1}
    assert shipped, ("no tracked files under connectors/ — did the built-ins move? "
                     "Every assertion here would pass trivially.")
    extra = reincluded - shipped
    missing = shipped - reincluded
    assert not extra, (
        f"{sorted(extra)} are re-included into the Docker build context by "
        ".dockerignore but are NOT tracked, so they are this box's own apps and "
        "would ship inside the image as built-ins. Delete those `!connectors/...` "
        "lines.")
    assert not missing, (
        f"{sorted(missing)} are shipped built-ins but .dockerignore no longer "
        "re-includes them, so the image would start without them. Add "
        "`!connectors/<name>` for each.")
