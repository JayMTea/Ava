"""Emptying a store: what it removes, what it refuses, and what it must not touch.

The README markets this page's "audited deletes" and there was no DELETE endpoint
for any store at all — emptying one meant `rm` by hand under `AVA_HOME`, with no
record and a real chance of bricking the install.

Two properties carry the weight here:

  * **Three stores refuse, each with a store-specific reason.** A refusal that
    cannot say why is indistinguishable from an oversight, and the reasons are not
    interchangeable: the ledger records the deletions *this page performs*,
    `secrets` holds the credentials that let you log in at all, and the voiceprint
    has a dedicated path that also resets its derived threshold and evicts the
    process's cached copy.
  * **`performance` and `alloc` share `logs_dir()` with `audit.jsonl`.** A blanket
    directory walk to empty either one would delete the single store that must
    never be deleted — while the API was still refusing an explicit request to
    delete it. That is the whole test below.
"""
import ast
import json

import pytest

from ava_bridge import audit, data_api, settings


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway AVA_HOME with the log stores populated."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setattr(settings, "AVA_HOME", tmp_path, raising=False)
    logs = tmp_path / "logs"
    (logs / "hw_history").mkdir(parents=True)
    (logs / "devices").mkdir(parents=True)
    (logs / "rollups").mkdir(parents=True)
    monkeypatch.setattr(settings, "logs_dir", lambda: str(logs))
    monkeypatch.setattr(settings, "media_dir", lambda: str(tmp_path / "media"))
    monkeypatch.setattr(settings, "upload_dir", lambda: str(tmp_path / "uploads"))
    (tmp_path / "media").mkdir()
    (tmp_path / "uploads").mkdir()
    monkeypatch.delenv("AVA_PERF_LOG_DIR", raising=False)

    for rel, body in (
        ("audit.jsonl", '{"ts":1,"kind":"turn"}\n'),
        ("performance.jsonl", '{"ts":1}\n'),
        ("performance.jsonl.1", '{"ts":0}\n'),
        ("alloc.jsonl", '{"ts":1}\n'),
        ("hw_history/hw_1m.jsonl", '{"ts":1}\n'),
        ("devices/phone.jsonl", '{"ts":1}\n'),
        ("rollups/perf_daily.jsonl", '{"ts":1}\n'),
    ):
        (logs / rel).write_text(body, encoding="utf-8")
    (tmp_path / "media" / "a.png").write_bytes(b"x" * 10)
    (tmp_path / "uploads" / "b.txt").write_bytes(b"y" * 5)
    monkeypatch.setattr(audit, "_path", lambda: str(logs / "audit.jsonl"))
    return tmp_path, logs


# ---- the refusals ----------------------------------------------------------- #
@pytest.mark.parametrize("sid", ["audit", "secrets", "models"])
def test_the_protected_stores_refuse_with_a_reason(sid) -> None:
    r = data_api.delete_store(sid)
    assert r["ok"] is False and r["refused"] is True
    assert len(r["error"]) > 60, (
        f"{sid} refused without explaining why: {r['error']!r}. A bare refusal is "
        "indistinguishable from an oversight.")


def test_each_refusal_reason_is_specific_to_its_store() -> None:
    """Not one blanket 'dangerous' message pasted three times."""
    reasons = {sid: data_api.delete_store(sid)["error"]
               for sid in ("audit", "secrets", "models")}
    assert len(set(reasons.values())) == 3, reasons
    assert "ledger" in reasons["audit"].lower()
    assert "log" in reasons["secrets"].lower() or "password" in reasons["secrets"].lower()
    assert "voiceprint" in reasons["models"].lower()


def test_the_secrets_reason_does_not_claim_the_password_is_hashed() -> None:
    """This module's own inventory label corrects that overstatement; so must this.

    `_SECRET_LABELS` says "stored 0600, not hashed", with a comment recording that
    claiming "hashed" overstated the protection. A refusal message saying otherwise
    would reintroduce the same false claim two hundred lines away.
    """
    reason = data_api.delete_store("secrets")["error"]
    assert "not hashed" in reason.lower(), reason


def test_an_unknown_store_is_an_error_not_a_silent_success() -> None:
    r = data_api.delete_store("does-not-exist")
    assert r["ok"] is False and not r.get("refused")


# ---- the property that matters ---------------------------------------------- #
def test_emptying_performance_does_not_touch_the_audit_ledger(home) -> None:
    """`performance`, `alloc` and `audit` all live in logs_dir()."""
    _root, logs = home
    before = (logs / "audit.jsonl").read_text()

    r = data_api.delete_store("performance")
    assert r["ok"] and r["files"] >= 2, r
    assert (logs / "audit.jsonl").exists(), (
        "emptying the performance store deleted the audit ledger — they share "
        "logs_dir(), so a blanket directory walk destroys the one store the API "
        "explicitly refuses to delete")
    assert before in (logs / "audit.jsonl").read_text()
    assert not (logs / "performance.jsonl").exists()
    assert not (logs / "performance.jsonl.1").exists()
    assert not (logs / "rollups" / "perf_daily.jsonl").exists(), (
        "the rollups the performance store owns should go with it")
    assert (logs / "alloc.jsonl").exists(), "alloc is a different store"


def test_emptying_alloc_leaves_performance_and_audit_alone(home) -> None:
    _root, logs = home
    r = data_api.delete_store("alloc")
    assert r["ok"] and r["files"] == 1, r
    assert not (logs / "alloc.jsonl").exists()
    assert (logs / "audit.jsonl").exists()
    assert (logs / "performance.jsonl").exists()


def test_the_directory_survives_so_the_next_write_still_works(home) -> None:
    """Removing files, not the tree: something is usually watching the directory."""
    _root, logs = home
    data_api.delete_store("devices")
    assert (logs / "devices").is_dir(), (
        "the store directory was removed, so the next write fails with a "
        "permission/ENOENT error instead of simply starting from empty")


# ---- receipts and records --------------------------------------------------- #
def test_a_deletion_is_audited_with_the_store_NAMED(home) -> None:
    _root, logs = home
    data_api.delete_store("uploads")
    evts = [json.loads(ln) for ln in
            (logs / "audit.jsonl").read_text().splitlines() if ln.strip()]
    dd = [e for e in evts if e["kind"] == "data_delete"]
    assert dd, f"no data_delete record: {evts}"
    assert dd[-1]["store"] == "uploads", (
        "a record saying only that 'a store was emptied' cannot answer which one")
    assert dd[-1]["files"] >= 1 and dd[-1]["bytes"] >= 1


def test_the_receipt_reports_what_went(home) -> None:
    r = data_api.delete_store("media_gen")
    assert r["files"] == 1 and r["bytes"] == 10, r
    assert r["path"], "the receipt should name the directory it emptied"


def test_row_stores_report_rows_not_files(home, monkeypatch) -> None:
    from ava_bridge import memory_store
    monkeypatch.setattr(memory_store, "_LOCK", memory_store._LOCK)
    memory_store.add("fact", "one")
    memory_store.add("fact", "two")
    r = data_api.delete_store("memory")
    assert r["ok"] and r["rows"] == 2 and r["files"] == 0, r


# ---- the inventory agrees with the API -------------------------------------- #
def test_every_store_declares_whether_it_can_be_deleted() -> None:
    """The UI must never render a button the API will refuse."""
    for s in data_api.stores()["stores"]:
        assert "deletable" in s, f"{s['id']} does not say whether it is deletable"
        if not s["deletable"]:
            assert s["delete_refused_reason"], (
                f"{s['id']} is not deletable and gives no reason, so the UI can "
                "only show a disabled control with no explanation")
        got = data_api.delete_store(s["id"]) if not s["deletable"] else None
        if got is not None:
            assert got["error"] == s["delete_refused_reason"], (
                f"{s['id']}: the inventory's reason and the API's refusal differ — "
                "two copies of the same message that can drift")


# ---- the UI cannot offer what the API refuses -------------------------------- #
def test_the_panel_gates_its_delete_button_on_the_API_flag() -> None:
    """A static scan, because the alternative is a browser test for one boolean.

    The backend carries the policy (`deletable` + `delete_refused_reason`) so that
    the panel never has to know which stores are protected. That only holds while
    the button is actually gated on the flag — remove the gate and the UI happily
    offers an Empty button that the API answers with a 403, which reads to the
    owner as a broken product rather than as a deliberate refusal.
    """
    from tests import gitfiles

    gitfiles.require_git()
    rel = "frontend/src/components/data/DataView.tsx"
    assert rel in gitfiles.tracked(rel), f"{rel} is not tracked — did it move?"
    src = (gitfiles.ROOT / rel).read_text(encoding="utf-8")
    assert "s.deletable" in src, (
        "DataView no longer reads `deletable`, so the Empty button is not gated "
        "on the API's policy. Gate it: `{s.deletable ? <button…> : <badge>}`.")
    assert "delete_refused_reason" in src, (
        "the refusal reason is no longer shown, so a protected store looks "
        "arbitrarily disabled. Surface it as the title/tooltip.")
    body = src.split("function StoreRow", 1)[-1].split("\nfunction ", 1)[0]
    assert "s.deletable" in body, (
        "the `deletable` check left StoreRow — the button and its gate must sit "
        "together, or a later edit moves one without the other.")


def test_the_file_removal_helper_has_exactly_one_auditing_caller() -> None:
    """The premise behind `_rm_tree_files`'s entry in the audit guard's allowlist.

    It is exempt from `test_destructive_paths_audited` because it cannot write a
    useful record — it gets a root path and does not know which store it belongs
    to. That exemption is only safe while `delete_store()`, which does know and
    does audit, is its ONLY caller. A second caller elsewhere would delete owner
    files with no ledger record and the audit guard would stay green, because the
    allowlist entry already told it not to look.
    """
    from tests import gitfiles

    gitfiles.require_git()
    tree = ast.parse((gitfiles.ROOT / "ava_bridge" / "data_api.py")
                     .read_text(encoding="utf-8"))
    callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_rm_tree_files" for n in ast.walk(fn))
    }
    assert callers == {"delete_store"}, (
        f"_rm_tree_files() is now called from {sorted(callers)}. It is allowlisted "
        "in tests/test_destructive_paths_audited.py on the grounds that its only "
        "caller audits with the store named. Either audit in the new caller too, "
        "or drop the allowlist entry.")
