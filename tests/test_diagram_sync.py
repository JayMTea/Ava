"""Rendered diagrams must match their .d2 sources — the SSOT guard CI can run.

arch.py stamps every SVG it renders with `d2sum:<sha256 of the .d2>`. For each
tracked .d2 that has a tracked sibling .svg, the stamp must match the current
.d2 content. This needs neither the d2 binary nor the (gitignored, deployment
-specific) architecture.yaml, so it runs anywhere — including CI — and catches
"edited the .d2 / regenerated the source, forgot to re-render" before it ships
to the site. Deeper manifest-vs-code drift is enforced on the deployment box
(pre-commit hook + the bridge's arch_watch scheduler).
"""
import hashlib
import pathlib
import re
from gitfiles import tracked_paths as _tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]




def test_tracked_svgs_match_their_d2() -> None:
    pairs = []
    tracked_svgs = {p for p in _tracked("*.svg")}
    for d2 in _tracked("*.d2"):
        svg = d2.with_suffix(".svg")
        if svg in tracked_svgs:
            pairs.append((d2, svg))
    assert pairs, "no tracked .d2/.svg pairs found — did the diagrams move?"

    stale = []
    for d2, svg in pairs:
        m = re.search(rb"d2sum:([0-9a-f]{64})", svg.read_bytes())
        want = hashlib.sha256(d2.read_bytes()).hexdigest()
        if not m or m.group(1).decode() != want:
            stale.append(str(svg.relative_to(ROOT)))
    assert not stale, (
        f"stale rendered diagrams {stale} — run `python agent/docs/arch.py sync` "
        "and commit the re-rendered SVGs"
    )
