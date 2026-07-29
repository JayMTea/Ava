"""Convention guard: the platform matrix is complete, and its tiers are earned.

`deploy/platforms.conf` is the single source of truth for what Ava claims about
each hardware class. Two ways it can lie, both guarded here:

  1. **Silence.** A new platform class lands in `hwinfo.platform_id()` with no row,
     so `ava doctor` and the install table simply do not mention the machine
     someone is standing in front of. This is how the old hand-maintained tables
     drifted: `linux-amd` and `linux-intel` existed in neither, because neither
     table's author was the person who added the class.
  2. **An unearned tier.** `verified-on-device` is the whole point of the file, and
     it is one word to type. So a tier above `ci-simulated` must name evidence that
     EXISTS on disk — a committed report, or a real CI job. Reviewing that by eye
     is exactly the check this repo keeps finding did not happen.

Style follows tests/test_diagram_sync.py: a static scan over `git ls-files` that
needs no bridge, no GPU and no network, and whose assertion message is the fix
instruction.
"""
import ast

import pytest
from gitfiles import ROOT, require_git, tracked

from ava_bridge import platforms

_HWINFO = "ava_bridge/hwinfo.py"
_MEM_MODELS = {"unified", "discrete", "system", "unknown"}


def _platform_id_literals() -> set[str]:
    """Every platform class hwinfo can actually return, read from its source.

    Via the AST rather than a regex, after three false starts all worth recording
    because each pointed the blame at the conf while the conf was correct:

      1. a line-anchored regex missed `darwin-intel`, whose assignment wraps;
      2. widening to the function body then matched `"linux"` and `"darwin"` out
         of the `sysname ==` comparisons;
      3. walking the assignment's whole value picked up `"arm64"`/`"aarch64"` from
         inside a ternary's CONDITION.

    So this collects only what can actually be *assigned*: constants, and the
    branches of a ternary — never its test. `_collect` is deliberately narrow;
    an unrecognised expression contributes nothing rather than everything.
    """
    tree = ast.parse((ROOT / _HWINFO).read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "platform_id"), None)
    assert fn is not None, (
        f"no platform_id() found in {_HWINFO} — this guard would pass vacuously.")

    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_platform_id"
                for t in node.targets):
            _collect(node.value, out)
    return out


def _collect(value: ast.AST, out: set[str]) -> None:
    """String constants this expression can EVALUATE TO (not ones it reads)."""
    if isinstance(value, ast.Constant):
        if isinstance(value.value, str):
            out.add(value.value)
    elif isinstance(value, ast.IfExp):
        _collect(value.body, out)      # the ternary's branches only...
        _collect(value.orelse, out)    # ...never value.test


def test_every_platform_class_has_a_row() -> None:
    require_git()
    classes = _platform_id_literals()
    assert classes, (
        f"found no platform literals in {_HWINFO} — did _platform_id assignment "
        "change shape? Update _ASSIGNED, or this guard silently passes forever.")

    rows = platforms.load()
    assert rows, ("deploy/platforms.conf parsed to zero rows — the matrix is the "
                  "SSOT for `ava doctor`, install.sh and two docs tables.")

    covered = {p.platform_id for p in rows}
    missing = sorted(classes - covered)
    assert not missing, (
        f"hwinfo.platform_id() can return {missing} with no row in "
        "deploy/platforms.conf, so `ava doctor` and the install table would not "
        "describe that machine at all. Add a row (start at tier ci-simulated, "
        "evidence '-') — that is an honest claim and costs nothing.")

    # The reverse direction: a row for a class that no longer exists is a claim
    # about hardware the code cannot detect.
    stale = sorted(covered - classes)
    assert not stale, (
        f"deploy/platforms.conf has rows for {stale}, which hwinfo.platform_id() "
        "can never return. Remove them, or restore the class.")


def test_keys_are_unique() -> None:
    keys = [p.key for p in platforms.load()]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, (
        f"duplicate keys in deploy/platforms.conf: {dupes}. `key` is what evidence "
        "files and the docs tables join on, so a duplicate makes the join ambiguous.")


def test_tiers_and_mem_models_are_from_the_known_sets() -> None:
    bad_tier = [(p.key, p.tier) for p in platforms.load()
                if p.tier not in platforms.TIERS]
    assert not bad_tier, (
        f"unknown tier(s) {bad_tier}. Allowed: {list(platforms.TIERS)}. The tier "
        "vocabulary is closed on purpose — an ad-hoc word like 'mostly works' is "
        "how a matrix stops being checkable.")

    bad_mem = [(p.key, p.mem_model) for p in platforms.load()
               if p.mem_model not in _MEM_MODELS]
    assert not bad_mem, (
        f"unknown mem_model(s) {bad_mem}. Allowed: {sorted(_MEM_MODELS)} — this "
        "column is what disambiguates a GB10 from an RTX box, both 'linux-nvidia'.")


def test_a_tier_above_simulated_names_evidence_that_exists() -> None:
    require_git()
    problems: list[str] = []
    workflows = set(tracked(".github/workflows/*.yml"))
    for p in platforms.load():
        if p.tier not in platforms.EVIDENCE_REQUIRED:
            continue
        ev = (p.evidence or "").strip()
        if not ev or ev == "-":
            problems.append(f"{p.key}: tier {p.tier} cites no evidence")
            continue
        if p.tier == "ci-native":
            wf, _, job = ev.partition(":")
            rel = f".github/workflows/{wf}"
            if rel not in workflows:
                problems.append(f"{p.key}: no such workflow {rel}")
            elif job and f"\n  {job}:" not in (ROOT / rel).read_text(
                    encoding="utf-8"):
                problems.append(f"{p.key}: {rel} has no job '{job}'")
        else:
            if not (ROOT / ev).exists():
                problems.append(f"{p.key}: evidence file {ev} does not exist")
    assert not problems, (
        "these rows claim a verification tier with nothing behind it:\n  "
        + "\n  ".join(problems)
        + "\n\nEither produce the evidence (tools/ondevice_check.py --record --json "
          "writes docs/evidence/<key>-<date>.json) or lower the tier to "
          "ci-simulated. An unbacked 'verified-on-device' is worse than an honest "
          "'ci-simulated': it is the exact class of claim this repo's own doctrine "
          "exists to prevent.")


def test_committed_evidence_reports_match_their_row() -> None:
    """A report must say which platform it evidences, and agree with the table."""
    import json
    # Filesystem glob, not `git ls-files`: a freshly recorded report must be
    # validated BEFORE it is committed, which is exactly when a wrong
    # platform_key would otherwise slip through review into the table.
    reports = sorted(p.relative_to(ROOT)
                     for p in (ROOT / "docs" / "evidence").glob("*.json"))
    if not reports:
        pytest.skip("no evidence reports recorded yet")
    by_key = {p.key: p for p in platforms.load()}
    for rel in reports:
        data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        key = data.get("platform_key")
        assert key in by_key, (
            f"{rel} evidences platform_key {key!r}, which is not a key in "
            "deploy/platforms.conf. The report and the table must join.")
        row = by_key[key]
        assert row.evidence == str(rel), (
            f"{rel} evidences {key!r} but that row cites {row.evidence!r}. "
            "Point the row at this report, or delete the stale one.")
        got = (data.get("hwinfo") or {}).get("platform")
        assert got == row.platform_id, (
            f"{rel} was recorded on platform {got!r} but claims to evidence "
            f"{row.platform_id!r}. A report from the wrong machine is not evidence.")


def test_the_rendered_docs_tables_match_the_conf() -> None:
    """Both docs tables must be what --sync would write.

    Same shape as tests/test_diagram_sync.py: the derived artifact is checked
    against its source by a test that needs no toolchain, so "edited the table,
    forgot the conf" (or the reverse) fails here rather than shipping a docs page
    that contradicts `ava doctor`.
    """
    require_git()
    targets = [("hwinfo", "docs/HWINFO_VALIDATION.md"),
               ("install", "deploy/README.md")]
    stale = []
    for view, rel in targets:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert platforms.BEGIN.format(view=view) in text, (
            f"{rel} has no platforms:begin:{view} marker — the generated block "
            "was removed, so this table is now hand-maintained and will drift.")
        if platforms.splice(text, view) != text:
            stale.append(rel)
    assert not stale, (
        f"these rendered tables no longer match deploy/platforms.conf: {stale} — "
        "run `python3 -m ava_bridge.platforms --sync` and commit the result. "
        "Edit the conf, never the table.")


def test_the_platform_simulator_passes_for_every_simulable_row() -> None:
    """Run tools/platform_sim_audit.py in-process, so CI covers the whole chain.

    The unit tests cover the parsers; this covers the DECISIONS built on them —
    hwinfo -> model_fit -> platforms.detect, unmocked above the HAL. It is the
    only automated check that a matrix row and the code actually agree about a
    platform nobody here owns.

    Invoked as a subprocess rather than imported because the audit monkeypatches
    module globals and mutates os.environ; letting it do that inside the pytest
    process would leak platform state into every test that runs after it.
    """
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "tools/platform_sim_audit.py"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (
        "tools/platform_sim_audit.py failed — a simulated platform no longer "
        "reaches the conclusion deploy/platforms.conf claims for it:\n"
        + (r.stdout or "")[-3000:] + (r.stderr or "")[-1000:])
