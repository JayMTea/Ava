"""`ava attest` — the trust artifact for the product 100% of users actually run.

A single-host Ava is the free, permanently-open half of this project, and a claim
about verifiable honesty that only works with a control plane is a claim almost nobody
can check. So `ava-attest/1` exists, and it is a **strict subset** of
`ava-attest-multi/1`: four artifacts, because four are all one box can honestly
produce.

Two properties carry this file:

* **The absent artifacts are named, not missing.** `container`, `containment`,
  `ceiling` and `usage` each need a second party. A single host claiming any of them
  would be asserting isolation from itself, so they appear in `not_measured` with the
  reason.
* **The voiceprint digest is INCLUDED here and WITHHELD by a multi-tenant bundle**, and the
  asymmetry is deliberate: this is the owner attesting about their own box, where the
  digest is what makes destruction provable. `--redact-biometrics` is the switch for
  handing it to anyone else, and the CLI warns when it was not passed.
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from ava_bridge import attest  # noqa: E402

CONTROL_PLANE_ARTIFACTS = {"container", "containment", "ceiling", "usage"}


# ---- the subset relationship -------------------------------------------------- #
def test_it_produces_exactly_the_four_single_host_artifacts() -> None:
    b = attest.build(now=0.0)
    assert set(b["artifacts"]) == {"stores", "policies", "chain", "health"}


def test_the_control_plane_only_artifacts_are_NAMED_not_silently_absent() -> None:
    """A missing artifact and a deliberately-absent one look identical otherwise."""
    b = attest.build(now=0.0)
    why = " ".join(n["what"] + " " + n["why"] for n in b["summary"]["not_measured"])
    for name in ("container", "containment", "ceiling", "usage"):
        assert name in why, (
            f"{name} is absent from this bundle and unexplained. It needs a second "
            "party; say so, or a reader cannot tell an omission from an oversight.")
    assert "asserting isolation from itself" in why


def test_the_schema_declares_what_it_is_a_subset_of() -> None:
    b = attest.build(now=0.0)
    assert b["schema"] == "ava-attest/1"
    assert b["provenance"]["subset_of"] == "ava-attest-multi/1"


def test_the_summary_note_refuses_to_be_read_as_a_security_verdict() -> None:
    note = attest.build(now=0.0)["summary"]["note"]
    assert "not this box's security posture" in note
    assert "cannot measure its own isolation" in note


def test_there_is_no_top_level_pass_fail() -> None:
    s = attest.build(now=0.0)["summary"]
    for banned in ("ok", "pass", "passed", "secure", "contained", "verdict"):
        assert banned not in s, f"summary emits {banned!r}"


# ---- the state machine -------------------------------------------------------- #
@pytest.mark.parametrize("state", ["degraded", "unavailable", "unknown"])
def test_an_incomplete_artifact_must_say_why(state) -> None:
    with pytest.raises(ValueError, match="requires a reason"):
        attest.Artifact(name="x", state=state, evidence_class="inventory")


def test_an_unknown_state_or_class_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown state"):
        attest.Artifact(name="x", state="fine", evidence_class="inventory")
    with pytest.raises(ValueError, match="unknown evidence_class"):
        attest.Artifact(name="x", state="collected", evidence_class="proof")


def test_the_state_and_class_vocabularies_are_the_shared_ones() -> None:
    """Two schemas, one vocabulary — a reader should not have to relearn it."""
    assert attest.STATES == ("collected", "degraded", "unavailable", "unknown")
    assert attest.EVIDENCE_CLASSES == ("measurement", "configuration", "inventory")


# ---- the chain, and its honest limitation ------------------------------------- #
def test_the_chain_artifact_admits_it_is_self_reported() -> None:
    """A multi-tenant bundle has the control plane recompute this. One box has
    nobody to ask.

    Saying so is the difference between a limitation and a lie: a multi-tenant bundle
    refuses to ask Ava whether Ava's ledger is intact, and here there is no
    alternative — so the caveat has to travel with the number.
    """
    art = attest.collect_chain()
    assert art.data.get("self_reported") is True
    assert "no second party to ask" in art.data.get("caveat", "")
    assert any("independent verification" in n["what"]
               for n in attest.NOT_MEASURED)


def test_an_unchained_ledger_is_degraded_not_collected() -> None:
    """The maintainer's own ledger holds thousands of pre-chain records."""
    art = attest.collect_chain()
    if (art.data or {}).get("state") == "unverifiable":
        assert art.state == "degraded", (
            "a ledger with no chained records was reported complete. Claiming to "
            "have checked records that predate the mechanism is the false assurance "
            "this whole track removes.")


def test_the_chain_rule_in_provenance_is_the_CORRECTED_one() -> None:
    """The rule Ava documented was false after a legacy tail; two implementations
    disagreeing is what found it."""
    rule = attest.provenance(0.0)["chain_rule"]
    assert "only if it is the first line" in rule
    assert "str.splitlines" in rule


# ---- biometrics: the asymmetry, and the switch -------------------------------- #
def test_the_digest_is_included_by_default_for_the_owners_own_box() -> None:
    b = attest.build(now=0.0)
    vp = b["artifacts"]["stores"]["data"]["stores"]["voiceprint"]
    if not vp["present"]:
        pytest.skip("no voiceprint enrolled on this box")
    assert vp["digest"], (
        "the digest is absent by default. On the owner's own box it is what makes "
        "destruction provable (docs/BIOMETRICS.md); a multi-tenant bundle withholds it "
        "because that one goes to a third party.")
    assert "before sharing" in vp["digest_note"]


def test_redact_biometrics_withholds_it_and_records_that_it_did() -> None:
    b = attest.build(redact_biometrics=True, now=0.0)
    vp = b["artifacts"]["stores"]["data"]["stores"]["voiceprint"]
    assert vp["digest"] is None
    assert "stable pseudonym" in vp["digest_withheld"]
    assert b["provenance"]["biometrics_redacted"] is True, (
        "a redacted bundle does not say it was redacted, so a recipient cannot tell "
        "a withheld digest from a box with no voiceprint")
    assert any("voiceprint" in n["what"] for n in b["summary"]["not_measured"])


def test_the_voiceprint_entry_points_at_the_destruction_path() -> None:
    vp = attest.build(now=0.0)["artifacts"]["stores"]["data"]["stores"]["voiceprint"]
    assert "Setup → Voice" in vp["destruction"]
    assert vp["special_category"].startswith("GDPR Art. 9")


# ---- no hardcoded filenames, no leaked credentials --------------------------- #
def test_the_store_inventory_asks_data_api_rather_than_guessing_paths() -> None:
    """The `chats.json` failure, which this project has now made twice."""
    src = (ROOT / "ava_bridge" / "attest.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    lits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    offenders = [x for x in lits if "/" in x and x.endswith((".json", ".db", ".jsonl"))
                 and not x.startswith(("ava_bridge/", "tools/", "docs/"))]
    assert not offenders, (
        f"filename literal(s) {offenders}. data_api.stores() returns each store's "
        "own `path`; guessing is how a probe came to read chats.json after the "
        "corpus became chats.db.")


def test_a_credential_shaped_value_is_withheld(monkeypatch) -> None:
    from ava_bridge import data_api

    monkeypatch.setattr(data_api, "stores",
                        lambda: {"stores": [{"id": "secrets",
                                             "password": "hunter2"}]})
    art = attest.collect_stores()
    assert art.state == "degraded" and art.data == {"withheld": True}


def test_provenance_withholds_host_identity() -> None:
    import getpass
    import socket

    p = attest.provenance(0.0)
    blob = json.dumps(p).lower()
    for leak in (socket.gethostname().lower(), getpass.getuser().lower()):
        if len(leak) > 3:
            assert leak not in blob, f"provenance leaks {leak!r}"
    assert "/home/" not in blob
    assert set(p["withheld"]) >= {"hostname", "username"}


# ---- the bundle and its verifier --------------------------------------------- #
def test_the_bundle_carries_a_verifier_that_imports_nothing_from_ava(tmp_path) -> None:
    b = attest.build(redact_biometrics=True, now=0.0)
    attest.write_bundle(b, str(tmp_path))
    v = tmp_path / "verify.py"
    assert v.is_file(), "the bundle has no verifier"
    tree = ast.parse(v.read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert imported <= {"hashlib", "json", "sys", "pathlib", "__future__"}, (
        f"verify.py imports {sorted(imported)}; it must be stdlib-only so a "
        "recipient can read all of it before running any of it")


def test_the_copied_verifier_matches_the_repos(tmp_path) -> None:
    b = attest.build(now=0.0)
    attest.write_bundle(b, str(tmp_path))
    assert (tmp_path / "verify.py").read_bytes() == (
        ROOT / "tools" / "verify_bundle.py").read_bytes(), (
        "the bundle's verify.py differs from tools/verify_bundle.py; the repo copy "
        "is the one that gets linted and tested")


def test_one_verifier_understands_both_schemas() -> None:
    src = (ROOT / "tools" / "verify_bundle.py").read_text(encoding="utf-8")
    assert '"ava-attest/1"' in src and '"ava-attest-multi/1"' in src, (
        "the shared verifier no longer accepts both schemas. Two verifiers drift, "
        "and a recipient should not have to know which bundle they were sent.")


def test_every_artifact_digest_recomputes(tmp_path) -> None:
    b = attest.build(now=0.0)
    attest.write_bundle(b, str(tmp_path))
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["artifacts"], "the manifest indexes nothing"
    for rel, meta in m["artifacts"].items():
        got = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
        assert got == meta["sha256"], f"{rel} does not recompute"
    side = (tmp_path / "manifest.sha256").read_text().strip()
    assert side == hashlib.sha256(
        (tmp_path / "manifest.json").read_bytes()).hexdigest()


def test_the_verifier_runs_clean_and_self_tests_on_a_real_bundle(tmp_path) -> None:
    b = attest.build(redact_biometrics=True, now=0.0)
    attest.write_bundle(b, str(tmp_path))
    clean = subprocess.run([sys.executable, "verify.py", "."], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=120)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    st = subprocess.run([sys.executable, "verify.py", ".", "--self-test"],
                        cwd=str(tmp_path), capture_output=True, text=True,
                        timeout=120)
    assert st.returncode == 0 and "SELF-TEST ok" in st.stdout, st.stdout


# ---- the CLI ------------------------------------------------------------------ #
def _cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "ava_cli.py"), *args],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=300)


def test_attest_is_registered_and_documents_its_flags() -> None:
    r = _cli("attest", "--help")
    assert r.returncode == 0, r.stderr
    for flag in ("--json", "--out", "--redact-biometrics"):
        assert flag in r.stdout, f"{flag} is undocumented"


def test_a_default_run_writes_nothing_on_an_initialised_ava(tmp_path) -> None:
    """Run it twice; the SECOND run must change nothing.

    The honest claim is narrower than "attest writes nothing". Against a FRESH
    `AVA_HOME` it creates `data/memory.db` and `data/.internal_token` — measured —
    because reading a SQLite store opens it, and opening it creates it. That is Ava's
    ordinary first-run initialisation, which any command triggers, not something
    `attest` does on its own.

    So the property worth asserting is that attest is idempotent on an initialised
    box: no ledger row, no cache, no report left behind. Comparing two consecutive
    runs proves that and cannot be satisfied by accident.
    """
    import os

    env = dict(os.environ, AVA_HOME=str(tmp_path))

    def _run():
        return subprocess.run([sys.executable, str(ROOT / "ava_cli.py"), "attest"],
                              capture_output=True, text=True, cwd=str(ROOT),
                              env=env, timeout=300)

    _run()                                       # first-run initialisation
    before = {(p, p.stat().st_size) for p in tmp_path.rglob("*") if p.is_file()}
    assert before, "the first run initialised nothing, so this test proves nothing"
    r = _run()
    assert r.returncode in (0, 1, 2), r.stderr
    after = {(p, p.stat().st_size) for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before, (
        f"a second `ava attest` changed {sorted(after ^ before)[:4]}. Reading is the "
        "whole contract: --out is the only thing that writes.")


def test_out_has_no_default() -> None:
    src = (ROOT / "ava_cli.py").read_text(encoding="utf-8")
    block = src.split('atp.add_argument("--out"', 1)[1].split(")", 1)[0]
    assert "default=" not in block, (
        "--out has a default; a plain `ava attest` must write nothing")


def test_the_cli_warns_before_you_share_an_unredacted_bundle() -> None:
    src = (ROOT / "ava_cli.py").read_text(encoding="utf-8")
    body = src.split("def cmd_attest(", 1)[1].split("\ndef ", 1)[0]
    assert "--redact-biometrics before sharing" in body, (
        "the CLI no longer warns that an unredacted bundle carries the voiceprint "
        "digest. Including it by default is only defensible if the owner is told.")
