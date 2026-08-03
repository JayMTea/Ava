"""A planning doc is written for the maintainer. It must not ship to users.

`docs/PACKAGING_PLAN.md` was public on GitHub from the repo's root commit until
2026-08-03. It was a living strategy document: locked commercial decisions, a
competitive read naming where Ava trails the incumbents, an inventory of the
project's own technical debt, a phase checklist whose unticked boxes advertised
what was not built yet, and a "risks & open questions" section asking whether
there would ever be a hosted option. Two more — `ARDUINO_SDK_PLAN.md` and
`WARM_AGENT_MODE.md` — shipped the same shape: phase status and a design sketch
for something not implemented.

None of them ever reached the docs SITE: `docs-site/sync.py` is an allow-list and
never staged them. That is exactly what made this hard to see. The site was
curated, the repo was not, and "not on the site" reads as "not published" right
up until someone opens the docs/ directory on GitHub.

So the allow-list needs a mirror on the git side, and it cannot be a list of
filenames — the next plan doc will have a name nobody has thought of yet. What
these documents have in common is a SHAPE, and the shape is what this scans for:

1. A status header declaring the doc a plan, a draft, a sketch, or unimplemented.
2. A title that announces itself as a plan or a roadmap (including `*_PLAN.md`).
3. A phase roadmap: unticked task boxes sitting under a "Phase N" heading.

Signal 3 is deliberately a CONJUNCTION. `.github/pull_request_template.md` and
`CONTRIBUTING.md` are both full of unticked boxes and neither is a roadmap; it is
the boxes-plus-phases pairing that means "this is the plan, and here is what is
still missing from it".

Scope note, in the tests/test_diagram_sync.py tradition of saying what a guard
cannot do: this reads tracked markdown only. A plan pasted into a Python
docstring, or into the CHANGELOG, or written without any of the three tells, gets
through. It catches the shape that actually shipped, which beats catching none of
them, and it costs a fork nothing — a fork has no plan docs to hide.

Run: .venv/bin/python -m pytest tests/test_no_internal_plans.py -q
"""
from __future__ import annotations

import re

from gitfiles import ROOT, tracked

# How much of a file counts as its header. A status line declares itself at the
# top or it is not a status line — a doc that says "status: draft" 300 lines in
# is describing a subsystem, not itself.
_HEADER_LINES = 15

# "Status: design only", "> **Status:** Living design document", "Status — draft".
_STATUS = re.compile(
    r"^\s*>?\s*\**status\**\s*[:—–-]\s*\**\s*"
    r"(design|draft|living|plan|proposal|sketch|not\s+implemented|planned)",
    re.IGNORECASE | re.MULTILINE)

# A title that announces itself. `# Plan: …`, `# Roadmap`, `# Design sketch — …`.
_PLAN_TITLE = re.compile(
    r"^#\s+(plan\b|roadmap\b|design sketch\b|.*\b(roadmap|productization)\b)",
    re.IGNORECASE | re.MULTILINE)

# Phrases a plan uses about itself. Prose ABOUT a plan ("documented as shipped,
# not on the roadmap") does not match: each of these needs the doc to be making
# the claim in the first person.
_PLAN_VOICE = re.compile(
    r"\b(this is the plan of record|plan of record for|living design document|"
    r"design only, not implemented|deferred to phase)\b", re.IGNORECASE)

_UNTICKED_BOX = re.compile(r"^\s*[-*]\s*\[ \]", re.MULTILINE)
_PHASE = re.compile(r"\bphase\s+\d", re.IGNORECASE)

# Escape hatch, empty on purpose. A doc that trips this guard is a doc that reads
# like a plan to a stranger too, and the fix is to move it out of the tree — not
# to name it here. If you add an entry, say in a comment why the doc is something
# a user needs and a maintainer does not.
_ALLOWED: set[str] = set()

_FIX = """
{rel} reads like an internal planning document ({why}).

Planning docs — roadmaps, phase checklists, design sketches, productization
strategy — are written for whoever maintains this project. Committing one
publishes the project's unbuilt features, its debt and its open commercial
questions to every user, forker and installer, and no docs-site allow-list will
catch it: the file is public the moment it is pushed.

Keep it OUTSIDE the tracked tree (a sibling directory works, so does anything
untracked). If the material belongs to users, rewrite it as documentation of what
Ava does today and put it in docs/ — then add it to docs-site/sync.py so it
actually reaches them. Adding an entry to _ALLOWED in this file is not the fix.
"""


def _why_it_looks_like_a_plan(rel: str, text: str) -> str:
    """The reason `text` reads as a plan, phrased for the failure message."""
    head = "\n".join(text.splitlines()[:_HEADER_LINES])
    if m := _STATUS.search(head):
        return f'a status header declaring it {m.group(1).lower()!r}'
    if rel.endswith("_PLAN.md") or rel.endswith("/PLAN.md") or rel == "PLAN.md":
        return "a filename ending in _PLAN.md"
    if m := _PLAN_TITLE.search(head):
        return f'its title: {m.group(0).strip()!r}'
    if m := _PLAN_VOICE.search(text):
        return f'it describes itself as a plan: {m.group(0).lower()!r}'
    if _UNTICKED_BOX.search(text) and _PHASE.search(text):
        return "unticked task boxes in a document organised by phase"
    return ""


def test_no_internal_planning_docs_are_tracked():
    for rel in tracked("*.md"):
        if rel in _ALLOWED:
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue                      # tracked but absent: nothing to read
        why = _why_it_looks_like_a_plan(rel, text)
        assert not why, _FIX.format(rel=rel, why=why)
