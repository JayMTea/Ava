"""A doc section the code sends people to must exist.

`setup_wizard.api_hardware` told every containerised user, in the panel copy
itself, to "see 'GPU telemetry in the bridge container' in deploy/README.md".
No such section existed — the override it describes is real, documented, and
lives in docs/INSTALL_REFERENCE.md §5 under a different name. So the pointer
looked like the most helpful line on the page and was the least: it named a
file, named a heading inside it, and sent the reader to search the wrong
350-line document for a heading that was never there.

It survived because nothing connects the two. `deploy/README.md` has no idea it
is being cited, and the citation is a string in a Python dict — so neither a
docs build nor a link checker nor any test could see the break. Prose citing
prose is exactly the reference a repo stops checking.

Scope note, in the tests/test_diagram_sync.py tradition of saying what a guard
cannot do: this catches the FORWARD form — a quoted title with a `.md` path
after it — because that is the shape that broke and the shape the UI copy uses.
`scaffold.py` also writes the reverse (path, then title), which this does not
parse. Widening it is welcome; a guard that catches one real class of break is
worth more than none while that is pending.

Run: .venv/bin/python -m pytest tests/test_doc_references.py -q
"""
import re

from gitfiles import ROOT, require_git, tracked_paths as _tracked

# A quoted section title, the word "in", then a markdown path. All three parts
# are load-bearing. Without the "in" this pairs any quoted string in the file
# with any nearby `.md` path, which matched twelve code fragments — dict keys,
# a `print(f` mid-expression, a comment about what a page is *about* — and not
# one real citation among them. The gaps allow for the markup a frontend puts
# in between: HardwarePanel's citation is `“…” in <code>deploy/README.md</code>`,
# split across three JSX lines.
_CITES = re.compile(
    r'["“]([A-Z][^"”\n]{5,79})["”][^"”\n]{0,40}?\bin\b[^"”\n]{0,40}?([\w./-]+\.md)\b')

# A heading is a short noun phrase. Anything carrying code punctuation is a
# fragment of source that happened to sit near a doc path.
_TITLEISH = re.compile(r"[\w'’./&—–\- ]+")

# Where a heading can live: a markdown heading, or a mkdocs admonition title
# (`??? note "…"` / `!!! note "…"`), which is how deploy/README.md structures
# most of its sections.
_HEADING = re.compile(r'^\s{0,4}#{1,6}\s+(.+?)\s*$|'
                      r'^\s{0,4}[?!]{3}\s*\w*\s*["“]([^"”]+)["”]', re.M)

_SOURCE_GLOBS = ("ava_bridge/**/*.py", "ava_bridge/*.py", "agent/**/*.py",
                 "*.py", "tools/*.py",
                 "frontend/src/**/*.tsx", "frontend/src/**/*.ts",
                 "ava_bridge/web/*.html")


def _norm(s: str) -> str:
    """Compare on meaning, not typography.

    `scaffold.py` cites "The tool facade — ava-tools/1" while the heading reads
    "The tool facade - `ava-tools/1`". An exact match would fail on the dash and
    the backticks, which is a difference no reader would notice or care about.
    """
    s = s.replace("`", " ").replace("§", " ")
    s = re.sub(r"[‐-―\-]+", " ", s)     # every dash is a space
    s = re.sub(r"[^\w\s/.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().casefold()


def _headings(rel: str) -> list[str]:
    path = ROOT / rel
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return [_norm(m.group(1) or m.group(2)) for m in _HEADING.finditer(text)]


def test_every_cited_doc_section_exists() -> None:
    require_git()
    offenders, checked = [], 0
    seen: set[str] = set()
    for f in (p for g in _SOURCE_GLOBS for p in _tracked(g)):
        rel_src = f.relative_to(ROOT).as_posix()
        if rel_src in seen or rel_src.startswith(("tests/", "qa/", "demo/")):
            continue
        seen.add(rel_src)
        # Collapse newlines so a citation broken across JSX lines still reads as
        # one sentence — which is how the copy renders, and how it breaks.
        text = re.sub(r"\s+", " ", f.read_text(encoding="utf-8", errors="replace"))
        for title, doc in _CITES.findall(text):
            # A path is only a citation if it resolves to a doc in this repo.
            if not (ROOT / doc).is_file():
                continue
            # A heading is 2-10 plain words. Bare filenames, code fragments and
            # anything punctuated like source are not section titles.
            if not _TITLEISH.fullmatch(title) or not 2 <= len(title.split()) <= 10:
                continue
            want = _norm(title)
            if not want or want.endswith(".md"):
                continue
            checked += 1
            if not any(want in h for h in _headings(doc)):
                offenders.append(f'{rel_src}: cites "{title}" in {doc}, '
                                 f"which has no such heading")

    assert not offenders, (
        "code sends readers to a doc section that does not exist. Write the "
        "section, or change the pointer to one that is there:\n  - "
        + "\n  - ".join(sorted(set(offenders))))
    assert checked, ("no citations parsed at all — the pattern stopped matching "
                     "and this guard is now asserting nothing")


def test_the_gpu_section_the_panel_promises_is_there() -> None:
    """The specific break, pinned.

    Setup → Hardware tells a containerised owner their card is fine and names
    this section as the way to surface it. Renaming or moving the section turns
    the most reassuring line in the panel back into a dead end — and this is a
    doc the panel cites by name, so it cannot move quietly.
    """
    require_git()
    # Substring, not equality: these headings are numbered ("## 5. Surfacing …"),
    # and the number is not part of what the panel cites.
    want = "surfacing the gpu in the bridge container"
    assert any(want in h for h in _headings("docs/INSTALL_REFERENCE.md")), (
        "Setup → Hardware cites this section by name; it is no longer in "
        "docs/INSTALL_REFERENCE.md under that title")
