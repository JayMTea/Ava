"""Every audit kind the backend emits has owner-facing copy in the frontend.

`audit.record("<kind>", …)` writes a machine token. Data -> History (the
ledger) and the Data -> Logs tails render that token through `EVENT_META` in
`frontend/src/components/hub/events.ts`, which gives it a glyph, a human label
and a tone. The module's own header says why it exists: "so the ledger reads
like the connectors/memory lists instead of raw event names".

The drift is SILENT, which is the whole reason this file exists. `eventMeta()`
falls back to `{icon: 'info', label: humanize(kind), tone: 'muted'}` for a token
it does not know — so a new `audit.record("brand_asset_delete", …)` renders as a
grey "Brand asset delete" with a generic glyph, beside rows that are properly
typed and toned. Nothing fails. Every test, lint and typecheck stays green, and
a destructive event is filed at the same visual weight as a passive one.

That is not hypothetical: when this guard was written, FOURTEEN of the
twenty-seven emitted kinds had no entry — including `data_delete`,
`memory_delete`, `connector_delete` and `secret`, every one of which should read
as destructive or security-relevant and instead read as "system noise".

Only STRING-LITERAL kinds are scanned. A computed kind (`f"alloc.{event}"` in
ava_bridge/alloc/broker.py) cannot be resolved statically, and a guard that
guessed at one would either invent tokens or quietly miss them; those are
covered by the literal `alloc.*` kinds their siblings emit.

Guard style: a static scan over `git ls-files` (see tests/gitfiles.py), so it
runs anywhere and needs no bridge, no browser and no node.
"""
import pathlib
import re

from gitfiles import tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

EVENTS_TS = "frontend/src/components/hub/events.ts"

# Where audit kinds are written from. Both are scanned whole: a kind emitted
# from anywhere reaches the same ledger and the same renderer.
CODE_ROOTS = ("ava_bridge", "agent")

# `audit.record("kind", …)` / `_audit.record("kind", …)` / `record(kind="…")`.
_EMIT = re.compile(r'(?:audit|_audit)\.record\(\s*(?:kind\s*=\s*)?"([a-z][a-z0-9_.]*)"')

# `  key: { … }` and `  'dotted.key': { … }` inside EVENT_META.
_META_KEY = re.compile(r"^\s{2}'?([a-z][a-z0-9_.]*)'?:\s*\{", re.M)


def _emitted() -> dict[str, str]:
    """Every literal audit kind the backend can write, and one file that does."""
    found: dict[str, str] = {}
    for rel in sorted(tracked()):
        if not rel.endswith(".py") or not rel.startswith(CODE_ROOTS):
            continue
        src = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for m in _EMIT.finditer(src):
            found.setdefault(m.group(1), rel)
    return found


def _known() -> set[str]:
    have = set(tracked())  # also skips cleanly outside a git checkout
    assert EVENTS_TS in have, (
        f"{EVENTS_TS} is not tracked — if the audit-event copy table moved, "
        "point this guard at its new home rather than deleting it")
    src = (ROOT / EVENTS_TS).read_text(encoding="utf-8")
    body = src.split("EVENT_META", 1)[-1].split("\n};", 1)[0]
    keys = set(_META_KEY.findall(body))
    assert keys, f"no `key: {{ … }}` entries found in EVENT_META ({EVENTS_TS})"
    return keys


def test_the_scan_finds_the_kinds_that_are_actually_there() -> None:
    """Without this, an emitter whose call shape changed makes the guard vacuous.

    A regex that matches nothing agrees with every copy table there is. These
    four are ordinary `audit.record("literal", …)` calls in four different
    modules; if they stop being found, the pattern is what broke, not the code.
    """
    emitted = _emitted()
    if not emitted:
        return  # not a git checkout — gitfiles.tracked() already skipped
    for kind in ("turn", "grant", "memory_distill", "memory_recall"):
        assert kind in emitted, (
            f"the audit-kind scan no longer finds `{kind}` — `_EMIT` has drifted "
            "from how ava_bridge writes audit events, so this guard is passing "
            "vacuously. Fix the pattern.")


def test_every_emitted_audit_kind_has_owner_facing_copy() -> None:
    emitted = _emitted()
    if not emitted:
        return  # not a git checkout
    missing = sorted(set(emitted) - _known())
    assert not missing, (
        "audit kinds with no EVENT_META entry: "
        + ", ".join(f"`{k}` ({emitted[k]})" for k in missing)
        + f". Each renders in Data -> History as a grey humanised token with a "
        f"generic glyph, which is exactly what {EVENTS_TS} exists to prevent. "
        "Add `<kind>: { icon, label, tone }` there — tone: accent = the agent "
        "changed something, warn/err = permission or destructive, ok = a normal "
        "turn, muted = passive/system.")


def test_no_copy_is_stranded_on_a_kind_nothing_emits() -> None:
    """The other direction: copy for an event that can no longer happen.

    Not an error — a kind can be retired ahead of its copy, and a stale entry
    renders nothing rather than something wrong. But it is worth naming, because
    a fixture written against a retired kind (this is how `route` outlived the
    thing that emitted it) reads as coverage while testing nothing.
    """
    emitted = _emitted()
    if not emitted:
        return  # not a git checkout
    stranded = sorted(_known() - set(emitted))
    assert not stranded, (
        f"EVENT_META in {EVENTS_TS} has copy for kinds nothing emits: "
        + ", ".join(f"`{k}`" for k in stranded)
        + ". Either the emitter was removed (drop the copy) or it is now written "
        "from a computed string this scan cannot see (say so in a comment here).")
