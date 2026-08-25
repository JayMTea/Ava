"""The frontend bundle has a ceiling, and it is a number rather than a worry.

The agent console roughly doubles the app's surface and brings the first new
runtime dependencies since the framework migration (xterm, for the terminal).
"keep an eye on the bundle" is not a check; a committed per-chunk ceiling is.

Why per-chunk and not a total: the whole point of the lazy boundaries is that a
chat-only session never downloads the charts, and a session that never opens a
terminal never downloads xterm. A total would go green by moving weight into a
chunk everybody loads, which is the opposite of the thing being protected.

Skips cleanly when `frontend/dist` has not been built — a Python-only checkout
should not fail on a missing artifact it never asked for.
"""
import gzip
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist" / "assets"

# gzip KiB. Set from measured sizes with room to grow, not aspirationally: a
# ceiling nobody can hit teaches people to ignore it.
BUDGET_KB = {
    # Everything a first paint needs. The one that matters most.
    "index": 120,
    "react": 70,
    # Behind lazy() — Vitals and Ops. Named so they share ONE recharts copy
    # instead of carrying one each.
    "charts": 140,
    # Behind lazy() — the terminal panel only.
    "term": 120,
    # The agent console itself, minus the two heavy things above.
    "AgentView": 40,
    # Behind lazy() — the Domains view. Feature is off by default, so most
    # installs should never download it. Measured 3.7 kB gz at first ship.
    "DomainsView": 20,
}


def _chunks() -> dict[str, int]:
    """{stem: gzipped bytes} for every built JS chunk, hash stripped."""
    out: dict[str, int] = {}
    for f in DIST.glob("*.js"):
        stem = re.sub(r"-[A-Za-z0-9_-]{6,}$", "", f.stem)
        out[stem] = max(out.get(stem, 0), len(gzip.compress(f.read_bytes(), 9)))
    return out


@pytest.mark.skipif(not DIST.is_dir(), reason="frontend/dist not built")
def test_the_scan_finds_the_bundle() -> None:
    """A budget over an empty directory passes vacuously."""
    got = _chunks()
    assert got, f"no .js chunks under {DIST} — did the build change its output dir?"
    assert "index" in got, f"no entry chunk found; saw {sorted(got)}"


@pytest.mark.skipif(not DIST.is_dir(), reason="frontend/dist not built")
def test_no_chunk_exceeds_its_budget() -> None:
    got = _chunks()
    over = []
    for stem, kb in BUDGET_KB.items():
        size = got.get(stem)
        if size is None:
            continue  # covered by the next test, which is about disappearance
        if size > kb * 1024:
            over.append(f"{stem}: {size / 1024:.1f} kB gz > {kb} kB")
    assert not over, (
        "a chunk grew past its committed ceiling:\n  " + "\n  ".join(over)
        + "\n\nEither split it behind a lazy() boundary, or raise the number in "
        "tests/test_bundle_budget.py deliberately — in the same commit, so the "
        "diff shows the growth next to what caused it.")


def _entry_mentions(needle: str) -> bool:
    """Is `needle` present in the ENTRY chunk's own bytes?

    Read from the built file rather than inferred from chunk names, because the
    failure this guards against is exactly a dependency losing its name by
    being folded into another chunk.
    """
    for path in DIST.glob("index-*.js"):
        if needle in path.read_text(encoding="utf-8", errors="ignore"):
            return True
    return False


@pytest.mark.skipif(not DIST.is_dir(), reason="frontend/dist not built")
def test_the_heavy_dependencies_stay_out_of_the_entry_chunk() -> None:
    """The lazy boundaries only pay off if they actually split.

    `manualChunks` WINS over a nested `lazy()`, so a component swept into a
    manual chunk can never be split out again. This is what catches the day
    somebody imports the terminal or a chart from a module the entry chunk
    reaches: the boundary silently stops existing and every visitor downloads
    it, and nothing else would say so.
    """
    got = _chunks()
    # `charts` must always split: something the entry chunk reaches imports it.
    assert "charts" in got, (
        "there is no separate `charts` chunk any more, so its weight has "
        "folded into something the entry chunk loads. Check "
        "frontend/vite.config.ts's manualChunks.")

    # `term` is the OTHER shape of correct. xterm is only pulled in by
    # frontend/src/lib/Terminal.tsx, and TerminalPanel currently gates instead
    # of rendering it (the gateway's terminal OUTPUT mechanism is uncaptured),
    # so nothing imports it and vite emits no chunk at all. Absent-entirely is
    # strictly better than split — the bytes are not shipped. What must never
    # happen is xterm coming back WITHOUT its boundary, so the assertion is
    # about the entry chunk, not about the chunk existing.
    if "term" not in got:
        assert not _entry_mentions("@xterm"), (
            "xterm has no `term` chunk yet its code reached the entry chunk — "
            "the lazy boundary stopped existing and every visitor now "
            "downloads a terminal. Check frontend/vite.config.ts.")

    entry = got.get("index", 0)
    assert entry < BUDGET_KB["index"] * 1024, (
        f"the entry chunk is {entry / 1024:.1f} kB gzipped")
