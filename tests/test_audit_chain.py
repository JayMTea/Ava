"""The audit chain, and the three answers it must be able to give.

`record()` swallows every exception so a ledger failure can never break serving.
That is the right trade and it has a cost: **a full disk silently drops events and
nothing can tell.** A sequence number makes a dropped record detectable, which is
the actual motivation here — tamper-evidence is the second-order benefit.

The case that earns its keep is `test_pre_chain_records_are_unverifiable_not_intact`.
The maintainer's own ledger holds 2,723 records written before the chain existed.
Folding those into "intact" would be a check reporting safety it never established
— the failure mode this repo's own doctrine enumerates — and it is also distinct
from "broken": nothing is wrong with a legacy record, we simply cannot speak for it.

Chain rule under test, restated so this file is a second independent statement of
it (see ava_bridge/audit.py):

    prev(record N) == sha256(exact written line of record N-1, newline excluded)
"""
import hashlib
import json

import pytest

from ava_bridge import audit


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A ledger at a temp path, with the actor context reset around each test."""
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "_path", lambda: str(path))
    tok = audit.set_actor("owner")
    yield path
    audit.reset_actor(tok)


def _lines(path) -> list[str]:
    # `"\n"` only. `splitlines()` also breaks on U+2028/U+2029/U+0085, and a record
    # written with ensure_ascii=False can carry a raw U+2028 in a label — so a
    # splitlines() helper sees two lines where one was written and every assertion
    # built on it is measuring the wrong thing. See test_a_record_containing_a_
    # unicode_line_separator_still_verifies.
    return [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]


def _events(path) -> list[dict]:
    return [json.loads(ln) for ln in _lines(path)]


# ---- the happy path --------------------------------------------------------- #
def test_a_fresh_chain_is_intact_and_sequential(ledger) -> None:
    for i in range(5):
        audit.record("turn", n=i)
    evts = _events(ledger)
    assert [e["seq"] for e in evts] == [1, 2, 3, 4, 5]
    assert evts[0]["prev"] == "", "the first chained record links to nothing"
    v = audit.verify_chain(str(ledger))
    assert v["state"] == "intact", v
    assert (v["chained"], v["first_seq"], v["last_seq"]) == (5, 1, 5)


def test_prev_is_the_hash_of_the_previous_LINE_not_a_reserialization(ledger) -> None:
    """The property that lets a third party verify with no copy of this module.

    Hashing a re-serialized object would make the check depend on JSON key order,
    separators and unicode escaping — three ways for an honest verifier to get a
    mismatch. Hashing the written bytes has none of those.
    """
    audit.record("a")
    audit.record("b")
    lines = _lines(ledger)
    want = hashlib.sha256(lines[0].encode("utf-8")).hexdigest()
    assert json.loads(lines[1])["prev"] == want


def test_the_ledger_is_verifiable_by_a_stranger_with_seven_lines(ledger) -> None:
    """A standalone verifier, written here from the documented rule alone."""
    for i in range(4):
        audit.record("turn", n=i)

    prev_line, ok = None, True
    for line in _lines(ledger):
        evt = json.loads(line)
        expect = "" if prev_line is None else \
            hashlib.sha256(prev_line.encode("utf-8")).hexdigest()
        ok &= evt["prev"] == expect
        prev_line = line
    assert ok, "the rule as documented does not reproduce the written chain"


# ---- the failure it exists to catch ----------------------------------------- #
def test_an_edited_record_breaks_the_chain_at_that_seq(ledger) -> None:
    for i in range(5):
        audit.record("turn", n=i)
    lines = _lines(ledger)
    evt = json.loads(lines[2])
    evt["n"] = 999                      # rewrite history
    lines[2] = json.dumps(evt, ensure_ascii=False)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    v = audit.verify_chain(str(ledger))
    assert v["state"] == "broken", (
        "an edited record left the chain reporting intact — a chain that cannot "
        "detect that is worse than no chain, because it invites belief")
    assert v["broken_at"] == 4, (
        f"the break should surface at the record AFTER the edit, got {v}")


def test_a_removed_record_is_detected_as_a_seq_gap(ledger) -> None:
    """The durability case: a dropped write, not an attack."""
    for i in range(5):
        audit.record("turn", n=i)
    lines = _lines(ledger)
    del lines[2]
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    v = audit.verify_chain(str(ledger))
    assert v["state"] == "broken"
    assert "seq jumped" in v["detail"], v


def test_a_truncated_ledger_is_still_intact(ledger) -> None:
    """Losing the TAIL is not corruption — logrotate does exactly this."""
    for i in range(5):
        audit.record("turn", n=i)
    lines = _lines(ledger)[:3]
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert audit.verify_chain(str(ledger))["state"] == "intact"


# ---- the legacy tier, which the real box exercises -------------------------- #
def test_pre_chain_records_are_unverifiable_not_intact(ledger) -> None:
    """2,723 real records predate the chain. They must not be claimed as verified."""
    legacy = [json.dumps({"ts": 1.0, "kind": "turn", "note": f"old {i}"})
              for i in range(3)]
    ledger.write_text("\n".join(legacy) + "\n", encoding="utf-8")
    audit.record("turn", n="new")

    v = audit.verify_chain(str(ledger))
    assert v["state"] == "intact", "the chained portion is fine"
    assert v["records"] == 4 and v["chained"] == 1
    assert v["unverifiable_before"] == 1
    assert "UNVERIFIABLE" in v["detail"], (
        f"the report must SAY the legacy records are unverifiable, not bury it in "
        f"a count: {v['detail']!r}")


def test_a_ledger_with_no_chain_at_all_says_so(ledger) -> None:
    legacy = [json.dumps({"ts": 1.0, "kind": "turn"}) for _ in range(3)]
    ledger.write_text("\n".join(legacy) + "\n", encoding="utf-8")
    v = audit.verify_chain(str(ledger))
    assert v["state"] == "unverifiable", (
        "a wholly pre-chain ledger is neither intact nor broken")
    assert v["chained"] == 0 and v["records"] == 3


def test_appending_after_legacy_records_starts_at_seq_1(ledger) -> None:
    """A legacy tail has no seq to increment from, so the chain begins fresh."""
    ledger.write_text(json.dumps({"ts": 1.0, "kind": "old"}) + "\n",
                      encoding="utf-8")
    audit.record("turn")
    assert _events(ledger)[-1]["seq"] == 1


# ---- actor ------------------------------------------------------------------ #
def test_the_actor_is_recorded_and_closed_to_a_known_set(ledger) -> None:
    audit.record("turn")
    assert _events(ledger)[-1]["actor"] == "owner"

    audit.record("turn", actor="agent")
    assert _events(ledger)[-1]["actor"] == "agent"

    audit.record("turn", actor="mallory")
    assert _events(ledger)[-1]["actor"] == "unknown", (
        "an unrecognised actor must become 'unknown' rather than be stored — the "
        "set is closed so the column stays answerable")


def test_an_unwired_call_site_reads_unknown_not_a_guess(ledger, monkeypatch) -> None:
    """Most call sites do not set an actor yet, and that must be visible.

    Defaulting to 'owner' or 'agent' would put a plausible-looking attribution on
    every legacy call site — a fabricated fact is worse than an admitted gap.
    """
    tok = audit.set_actor("")
    try:
        audit.record("turn")
    finally:
        audit.reset_actor(tok)
    assert _events(ledger)[-1]["actor"] == "unknown"


# ---- the contract the rest of the app depends on ---------------------------- #
def test_record_never_raises_even_when_the_ledger_is_unwritable(monkeypatch) -> None:
    """The property that lets every destructive path call this inline."""
    monkeypatch.setattr(audit, "_path", lambda: "/proc/definitely/not/writable")
    audit.record("turn", n=1)      # must not raise


def test_tail_still_works_and_returns_newest_first(ledger) -> None:
    for i in range(3):
        audit.record("turn", n=i)
    monkey = audit.tail(10)
    assert [e["n"] for e in monkey] == [2, 1, 0]
    assert all("seq" in e for e in monkey)


# ---- the two cases an independent implementation found ---------------------- #
def test_the_first_chained_record_after_a_LEGACY_tail_chains_to_it(ledger) -> None:
    """The docstring said `prev == ""` for the first chained record. It is false here.

    Every ledger that existed before the chain is in exactly this state: unchained
    records already on disk, then `record()` appends. `record()` hashes whatever line
    precedes it and does not inspect it for a `seq`, so the first chained record
    carries `prev` = the digest of the last LEGACY line — not "".

    Found by a second, independent implementation written from the documented rule
    alone, which reported `broken` at seq 1 where this module reports `intact`. Two
    implementations disagreeing is the entire reason for writing two.
    """
    ledger.write_text('{"ts": 1, "kind": "old"}\n{"ts": 2, "kind": "old"}\n',
                      encoding="utf-8")
    audit.record("turn", n="first-chained")
    audit.record("turn", n="second")

    evts = _events(ledger)
    first_chained = next(e for e in evts if "seq" in e)
    assert first_chained["seq"] == 1
    assert first_chained["prev"] != "", (
        "the first chained record after a legacy tail has prev == '' — either "
        "record() changed, or this ledger had no legacy tail and the fixture is "
        "not testing what it claims")
    lines = _lines(ledger)
    assert first_chained["prev"] == hashlib.sha256(
        lines[1].encode("utf-8")).hexdigest(), (
        "the first chained record does not chain to the legacy line before it")

    v = audit.verify_chain(str(ledger))
    assert v["state"] == "intact", (
        f"a legacy tail followed by a valid chain reported {v['state']!r}. That is "
        "the state of every ledger written before the chain existed, so this is "
        "the one case that must not be a false alarm.")
    assert v["unverifiable_before"] == 1, v


def test_a_record_containing_a_unicode_line_separator_still_verifies(ledger) -> None:
    """U+2028 is legal inside a JSON string and `str.splitlines()` breaks on it.

    A verifier that splits with `splitlines()` sees two lines where one was written
    and reports `broken` on an intact ledger. This module reads with `readlines()`
    and `rsplit("\n", 1)`, so it is unaffected — and this test pins that, because
    the failure is invisible until someone's prompt happens to contain the character.
    """
    audit.record("turn", label="before\u2028after")
    audit.record("turn", label="plain")

    raw = ledger.read_text(encoding="utf-8")
    assert "\u2028" in raw, "the fixture did not write a raw U+2028"
    assert len(raw.splitlines()) > len([x for x in raw.split("\n") if x.strip()]), (
        "splitlines() and split('\\n') agree on this file, so the test cannot "
        "detect a verifier that uses the wrong one")

    v = audit.verify_chain(str(ledger))
    assert v["state"] == "intact", (
        f"a record carrying U+2028 reported {v['state']!r}; the ledger is intact "
        "and the splitter is what is wrong")

    # And verify it the stranger's way, through `_lines` — which makes this test
    # fail if that helper goes back to `splitlines()`. Without this the helper's
    # splitter is untested hardening rather than a checked property.
    lines = _lines(ledger)
    assert len(lines) == 2, (
        f"_lines() returned {len(lines)} lines for 2 records — it is splitting on "
        "U+2028. Split on '\\n' only.")
    prev_line = None
    for line in lines:
        evt = json.loads(line)
        expect = "" if prev_line is None else hashlib.sha256(
            prev_line.encode("utf-8")).hexdigest()
        assert evt["prev"] == expect, (
            "the documented rule does not reproduce the chain over a record "
            "containing U+2028")
        prev_line = line
