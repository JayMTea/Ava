"""The session status vocabulary lives in exactly one module.

The mirror of `test_hub_uniformity.py::test_one_provisioning_vocabulary`, and
for the same reason: `DRIFT_LABEL`/`DRIFT_TONE` live only in `provisionView.ts`
so a status cannot be worded one way in a panel and another way in a badge.
Sessions have six states and they surface in the list, the thread header, the
nav rollup and (later) Activity — four chances to disagree.

The tone half is the sharper constraint. `tests/test_hub_uniformity.py` asserts
the stylesheet defines EXACTLY SIX `.tone-*` setters, so a seventh tone name
here renders an uncoloured dot and fails nothing at build time.

Style: `git ls-files` + regex. No build, no browser.
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT_DIR = "frontend/src/components/agent"
VIEW = f"{AGENT_DIR}/agentView.ts"
TONES = {"muted", "accent", "ok", "warn", "err", "info"}

# Two patterns, because the two maps hold different kinds of value. A tone is a
# single bare word; a LABEL is human text and may contain spaces ("needs you") —
# a shared `'([a-z]+)'` silently skipped that entry and reported the two maps as
# disagreeing when they did not.
_TONE_ENTRY = re.compile(r"^\s*'?([a-z-]+)'?:\s*'([a-z]+)'\s*,", re.M)
_LABEL_ENTRY = re.compile(r"^\s*'?([a-z-]+)'?:\s*'([^']+)'\s*,", re.M)


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _block(src: str, name: str) -> str:
    start = src.index(name)
    return src[start:src.index("};", start)]


def test_the_scan_finds_the_vocabulary() -> None:
    """A guard with no subjects agrees with everything."""
    assert VIEW in _tracked(f"{AGENT_DIR}/*.ts"), (
        f"{VIEW} is not tracked — point this guard at its new home rather than "
        "deleting it")
    src = _read(VIEW)
    for name in ("SESSION_TONE", "SESSION_LABEL"):
        assert name in src, f"{name} is gone from {VIEW}"


def test_every_session_tone_is_one_the_stylesheet_defines() -> None:
    tones = {t for _, t in _TONE_ENTRY.findall(_block(_read(VIEW), "SESSION_TONE"))}
    assert tones, "no entries parsed out of SESSION_TONE — fix the pattern"
    extra = tones - TONES
    assert not extra, (
        f"SESSION_TONE uses {sorted(extra)}, which hub.css does not define. "
        "There are exactly six `.tone-*` setters and test_hub_uniformity.py "
        "keeps it that way — a seventh name renders an uncoloured dot and "
        "fails nothing at build time. Map the new status onto an existing tone.")


def test_the_tone_and_label_maps_cover_the_same_states() -> None:
    """A state with a colour and no word renders a bare dot; a state with a word
    and no colour renders an uncoloured one. Both are half-built."""
    src = _read(VIEW)
    tones = {k for k, _ in _TONE_ENTRY.findall(_block(src, "SESSION_TONE"))}
    labels = {k for k, _ in _LABEL_ENTRY.findall(_block(src, "SESSION_LABEL"))}
    assert tones == labels, (
        f"SESSION_TONE and SESSION_LABEL disagree: only in tones {sorted(tones - labels)}, "
        f"only in labels {sorted(labels - tones)}")


def test_no_other_agent_module_maps_a_status_to_a_tone() -> None:
    """One module owns the mapping. A second copy is how a session reads
    "failed" in the list and "error" in the header."""
    offenders = []
    pattern = re.compile(r"(running|queued|needs-input|archived)'?\s*:\s*'(ok|warn|err|accent|muted|info)'")
    for rel in _tracked(f"{AGENT_DIR}/*.ts") + _tracked(f"{AGENT_DIR}/*.tsx"):
        if rel == VIEW or rel.endswith(".test.ts"):
            continue
        if pattern.search(_read(rel)):
            offenders.append(rel)
    assert not offenders, (
        "these modules map a session status to a tone themselves: "
        + ", ".join(offenders)
        + f". Import SESSION_TONE / SESSION_LABEL from {VIEW} instead.")


def test_no_agent_component_hardcodes_a_semantic_colour() -> None:
    """The tone system exists so a status colour is never a literal. A hex in a
    component is invisible to the theme and to a re-brand."""
    offenders = []
    hexes = re.compile(r"#(?:e0364d|3fb27f|e0a93b|007acc|7ea6c9)", re.I)
    for rel in _tracked(f"{AGENT_DIR}/*.tsx") + _tracked(f"{AGENT_DIR}/*.ts"):
        if hexes.search(_read(rel)):
            offenders.append(rel)
    assert not offenders, (
        "a semantic palette colour is hardcoded in: " + ", ".join(offenders)
        + ". Use a `.tone-*` class or var(--ok|--warn|--err|--accent|--info).")
