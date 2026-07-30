"""The voiceprint must be destroyable, and the destruction must actually hold.

Before this there was no code path to delete a voiceprint at all — not a route,
not a function. A repo-wide grep for voiceprint ∩ (delete|remove|unlink|forget)
returned nothing, while the artifact is GDPR Art. 9 special-category data and
Illinois BIPA §15(a) requires a written retention-and-destruction policy for it.

**The assertion that matters is `spk.load_voiceprint() is None`, never a path
check.** `load_voiceprint()` migrates a legacy repo-local copy forward when the
live one is absent — its own comment says "keep the legacy copy" — so deleting
one path lets the biometric come back on the next gate check. A test asserting
`not os.path.exists(VOICEPRINT)` passes on that broken implementation.

And the paths must be forced APART. On a default bare-metal install `AVA_HOME` is
the code root, so both collapse to one file and the bug is invisible; they diverge
under Docker, which is the primary documented install. A test written the obvious
way on the maintainer's own box would not have caught this.
"""
import json

import numpy as np
import pytest

import speaker as spk
from ava_bridge import state, voice_enroll


@pytest.fixture
def split_paths(tmp_path, monkeypatch):
    """Force VOICEPRINT and _LEGACY_VOICEPRINT to be DIFFERENT files.

    This is the configuration the bug lives in (AVA_HOME != code root, i.e. the
    Docker layout). Patching the module constants rather than only the env,
    because both are computed at import time.
    """
    live = tmp_path / "home" / "models" / "voiceprint.npy"
    legacy = tmp_path / "code" / "models" / "voiceprint.npy"
    live.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    monkeypatch.setattr(spk, "VOICEPRINT", str(live))
    monkeypatch.setattr(spk, "_LEGACY_VOICEPRINT", str(legacy))
    assert str(live) != str(legacy), "the fixture failed to split the paths"
    return live, legacy


def _plant(path, seed=1) -> None:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=192).astype(np.float32)
    v /= np.linalg.norm(v)
    np.save(str(path), v)


# ---- the resurrection bug --------------------------------------------------- #
def test_deleting_leaves_no_loadable_voiceprint(split_paths) -> None:
    live, legacy = split_paths
    _plant(live, 1)
    _plant(legacy, 2)
    assert spk.load_voiceprint() is not None, "fixture did not enrol anything"

    spk.delete_voiceprint()

    assert spk.load_voiceprint() is None, (
        "the voiceprint came back. load_voiceprint() migrates the legacy "
        "repo-local copy forward when the live one is absent, so removing one "
        "path resurrects the biometric on the next gate check. A path-existence "
        "check passes on that implementation; this assertion does not.")


def test_both_paths_are_reported_as_removed(split_paths) -> None:
    live, legacy = split_paths
    _plant(live, 1)
    _plant(legacy, 2)
    got = spk.delete_voiceprint()
    assert sorted(got["removed"]) == sorted([str(live), str(legacy)]), got
    assert not got["failed"]


def test_an_absent_copy_is_absent_not_failed(split_paths) -> None:
    """A box that never had the legacy layout must not look like a failure."""
    live, legacy = split_paths
    _plant(live)
    got = spk.delete_voiceprint()
    assert got["removed"] == [str(live)]
    assert got["absent"] == [str(legacy)]
    assert not got["failed"]


def test_voiceprint_paths_dedupes_the_collapsed_case(monkeypatch, tmp_path) -> None:
    """On bare metal both constants are the same file; report it once."""
    p = tmp_path / "voiceprint.npy"
    monkeypatch.setattr(spk, "VOICEPRINT", str(p))
    monkeypatch.setattr(spk, "_LEGACY_VOICEPRINT", str(p))
    assert spk.voiceprint_paths() == [str(p)]


# ---- the in-memory copy ----------------------------------------------------- #
def test_the_gate_cache_is_cleared_as_a_PAIR() -> None:
    """phone_bridge guards on `verifier` and dereferences `voiceprint`.

        if state.heavy["verifier"] is not None:
            sim = spk.cosine(state.heavy["voiceprint"], ...)

    so clearing the print alone turns every voice turn into a TypeError instead of
    the documented fail-open.
    """
    state.heavy["voiceprint"] = object()
    state.heavy["verifier"] = object()
    try:
        held = state.clear_voice_gate()
        assert held == {"voiceprint": True, "verifier": True}
        assert state.heavy["voiceprint"] is None
        assert state.heavy["verifier"] is None, (
            "the verifier survived the voiceprint — the next voice turn would "
            "pass None into spk.cosine()")
    finally:
        state.heavy["voiceprint"] = None
        state.heavy["verifier"] = None


# ---- the receipt ------------------------------------------------------------ #
def test_the_receipt_names_what_it_did_not_destroy(split_paths, monkeypatch,
                                                   tmp_path) -> None:
    """A receipt claiming more than it did is the failure mode here."""
    live, _ = split_paths
    _plant(live)
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "home"))
    r = voice_enroll.delete(reset_threshold=False)
    assert r["ok"] is True
    assert "models/ecapa/**" in r["not_destroyed"], (
        "the ECAPA weights are deliberately kept — say so, rather than leaving "
        "the owner to infer it from an 80 MB directory that survived")
    assert "enroll/" in r["not_destroyed"]
    assert r["digest_before"], "no pre-delete digest, so destruction is unevidenced"


def test_the_deletion_is_audited_with_a_digest(split_paths, tmp_path,
                                               monkeypatch) -> None:
    """The ledger must be able to say a specific artifact existed and went."""
    live, _ = split_paths
    _plant(live)
    ledger = tmp_path / "audit.jsonl"
    from ava_bridge import audit
    monkeypatch.setattr(audit, "_path", lambda: str(ledger))

    digest = spk.voiceprint_digest()
    assert digest, "a stored voiceprint must hash to something"
    voice_enroll.delete(reset_threshold=False)

    evts = [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]
    vp = [e for e in evts if e["kind"] == "voiceprint"]
    assert vp, f"no voiceprint event in the ledger: {evts}"
    assert vp[-1]["action"] == "delete"
    assert vp[-1]["digest_before"] == digest, (
        "the record must carry the PRE-delete digest — afterwards there is "
        "nothing left to hash, so a record written later cannot evidence anything")


def test_the_digest_is_not_the_voiceprint(split_paths) -> None:
    """A hash is recordable; the vector is not."""
    live, _ = split_paths
    _plant(live)
    d = spk.voiceprint_digest()
    raw = live.read_bytes()
    assert len(d) == 16 and all(c in "0123456789abcdef" for c in d)
    assert d.encode() not in raw


# ---- the inventory ---------------------------------------------------------- #
def test_the_data_page_lists_the_biometric_store() -> None:
    """The Data tab is documented as every store Ava keeps; this one was missing."""
    from ava_bridge import data_api
    ids = {s["id"] for s in data_api.stores()["stores"]}
    assert "models" in ids, (
        "the store holding the voiceprint is absent from the inventory the README "
        "calls 'the transparency page a cloud assistant can't give you'")
