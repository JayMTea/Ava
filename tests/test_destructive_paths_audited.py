"""Convention guard: a function that destroys owner data records that it did.

`docs/MEMORY.md` says edits and deletions "are logged the same way", and the README
markets the Data tab's "audited deletes". Both were true of the routes and false of
the code beneath them:

  * `memory_store.delete_item` and `delete_source` contained no audit call. The
    audit lived one layer up, in `hub/memory.py`'s handler — so any OTHER caller
    deleted owner memories silently. One already did: `index_document` calls
    `delete_source` on every re-upload of the same filename, which made the single
    most frequent deletion path on the box completely invisible, and not even
    user-initiated.
  * `hub/connectors.py`'s delete handler removed a connector's **generated egress
    policy** — the thing that bounds what the sandboxed agent may reach for that
    app — with nothing in the ledger. The security posture of the box changed and
    the flight recorder said nothing.

So the rule is enforced where the act happens, not where it is requested. A guard
on the routes would have passed throughout.

Style follows tests/test_alloc_measurement.py: a static scan over `git ls-files`
that runs anywhere, with the fix instruction in the assertion.
"""
import ast

from gitfiles import ROOT, tracked

# Calls that destroy persisted owner data. `truncate`/`unlink` are included even
# though nothing uses them today: the point of a convention guard is the case
# nobody has written yet.
_DESTRUCTIVE = {
    "rmtree", "remove", "unlink", "truncate",
}
_DESTRUCTIVE_SQL = ("delete from", "drop table", "truncate table")

# Functions that legitimately destroy something WITHOUT an owner-data trace, each
# with the reason. This is the `test_path_roots._ENV_ALLOW` pattern: an allowlist
# entry must say why, so the next reader can judge it rather than trust it.
_ALLOW = {
    # --- temp files this process created moments earlier ---------------------- #
    # Auditing a tempfile cleanup would bury the real events in noise.
    ("ava_bridge/audio.py", "_run_to_wav"),
    ("ava_bridge/audio.py", "decode_to_pcm"),
    ("ava_bridge/documents.py", "_office_text"),      # temp extraction dir
    ("ava_bridge/settings.py", "_write_config"),      # the .tmp of an atomic write
    ("ava_bridge/perf_store.py", "_write"),           # same: the mkstemp of an atomic
                                                      # rollup write, unlinked only on
                                                      # the failure path. Ava's own
                                                      # telemetry, never owner data.
    ("ava_bridge/alloc/ledger.py", "_writable"),      # a write-probe file

    # --- a helper whose only caller audits, with the store NAMED -------------- #
    # `_rm_tree_files` receives a bare root path and does not know which store it
    # belongs to, so its own record could only say "some files were deleted" —
    # the useless record test_store_deletion rejects. `delete_store()` is the
    # layer that knows the store id, and it is the only caller; that premise is
    # asserted by test_the_file_removal_helper_has_exactly_one_auditing_caller.
    ("ava_bridge/data_api.py", "_rm_tree_files"),

    # --- Ava's OWN telemetry, rotated on a retention policy ------------------- #
    # Not owner content, and governed by data.retention_days. A record per
    # rotation would be the loudest and least informative thing in the ledger.
    ("ava_bridge/app_perf.py", "_rotate_if_needed"),
    ("ava_bridge/devices.py", "_rotate_if_needed"),
    ("ava_bridge/alloc/broker.py", "_rotate_log_if_needed"),
    ("ava_bridge/perf_store.py", "_prune"),
    ("ava_bridge/perf_store.py", "_prune_raw"),
    ("ava_bridge/hardware.py", "_prune"),

    # --- writes its own richer record instead --------------------------------- #
    # data_maintenance carries counts and bytes; a second per-file record would
    # duplicate it less usefully.
    ("ava_bridge/data_api.py", "prune_media"),
    # policy_mgmt keeps logs/policy_changes.log, its own dedicated trail.
    ("ava_bridge/policy_mgmt.py", "update_policy"),

    # --- the self-edit path, audited as one `code_change` event --------------- #
    # These are helpers inside an operation the applier already records whole.
    # Recording each file operation would fragment one reviewable change into a
    # dozen rows that no longer add up to a diff.
    ("ava_bridge/code_agent.py", "_apply_edits"),
    ("ava_bridge/code_agent.py", "_apply_on_branch"),
    ("ava_bridge/code_agent.py", "_restore"),
    ("ava_bridge/coder.py", "apply_turn"),

    # --- allocator state, not owner data ------------------------------------- #
    ("ava_bridge/alloc/capacity.py", "reset_baseline"),
    # Lease lifecycle files under run/. Ephemeral coordination state that the
    # allocator's own alloc.jsonl already records; nothing here is owner content.
    ("ava_bridge/alloc/ledger.py", "update_lease"),
    ("ava_bridge/alloc/ledger.py", "drop_lease"),

    # --- consumed one-time tokens -------------------------------------------- #
    # The first-run claim token, removed once a password exists. Its whole life is
    # bounded by setup and the event that matters (a password being set) is already
    # recorded; a record for retiring a spent token is bookkeeping about bookkeeping.
    ("ava_bridge/auth.py", "clear_claim"),

    # --- a TEST utility, and its docstring says so --------------------------- #
    # `chat_store.reset()` drops every conversation, which sounds exactly like the
    # thing this guard is for — but every caller is under tests/ or qa/ (verified),
    # and its docstring reads "Used by tests to start from a known corpus". If a
    # product path ever calls it, remove this entry rather than widening it.
    ("ava_bridge/chat_store.py", "reset"),

    # This file names the calls it bans.
    ("tests/test_destructive_paths_audited.py", "*"),
}


def _audits(node: ast.FunctionDef) -> bool:
    """Does this function record the act — directly, or via an audit helper?

    Helper delegation has to count, or the guard forces every destructive function
    to inline `audit.record` even where a wrapper is the right shape.
    `settings.delete_backend_key` calls `_audit_secret(...)`, which exists so a
    credential's NAME is recorded and its VALUE never can be.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        # audit.record(...) / _audit.record(...)
        if isinstance(f, ast.Attribute) and f.attr == "record":
            owner = getattr(f.value, "id", "") or getattr(f.value, "attr", "")
            if "audit" in owner:
                return True
        # a local helper whose name says it audits, e.g. _audit_secret(...)
        name = getattr(f, "id", "") or getattr(f, "attr", "")
        if name.startswith("_audit") or name.endswith("_audit"):
            return True
    return False


def _destroys(node: ast.FunctionDef) -> tuple[str, str]:
    """`(what, which_matcher)` for the destructive act, or `("", "")`.

    The matcher is returned so the caller can prove EACH one still finds something.
    A single `scanned` counter was not enough: emptying `_DESTRUCTIVE` left the SQL
    matcher keeping the count non-zero, so the guard passed while no longer checking
    `rmtree`/`unlink` at all — coverage that had quietly become decoration.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name in _DESTRUCTIVE:
                return f"{name}()", "call"
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            low = sub.value.strip().lower()
            for sql in _DESTRUCTIVE_SQL:
                if low.startswith(sql):
                    return sub.value.strip()[:48], "sql"
    return "", ""


def test_functions_that_destroy_owner_data_write_to_the_ledger() -> None:
    found = {"call": 0, "sql": 0}
    unaudited: list[str] = []

    for rel in tracked("ava_bridge/*.py") + tracked("ava_bridge/**/*.py"):
        try:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            what, matcher = _destroys(node)
            if not what:
                continue
            found[matcher] += 1
            if (rel, node.name) in _ALLOW or (rel, "*") in _ALLOW:
                continue
            if not _audits(node):
                unaudited.append(f"{rel}:{node.lineno} {node.name}() — {what}")

    # EACH matcher independently, not their sum: see _destroys' docstring.
    for matcher, n in found.items():
        assert n, (
            f"the {matcher!r} matcher found nothing in ava_bridge/ — it has stopped "
            "checking. Either the pattern list went stale (a rename?) or the code "
            "genuinely has none left; verify which, because a matcher that matches "
            "nothing passes forever while looking exactly like coverage.")

    assert not unaudited, (
        "these destroy persisted data with no ledger record:\n  "
        + "\n  ".join(sorted(unaudited))
        + "\n\nCall audit.record() in the function that performs the destruction, "
          "not in the route above it:\n"
          "    audit.record('<kind>_delete', ..., rows=n, reason=...)\n"
          "\nPutting it in the route is what let index_document() bulk-delete "
          "owner memories on every document re-upload with no trace at all — the "
          "route was audited and the store was not. If a case genuinely needs no "
          "owner-data record (a tempfile, Ava's own telemetry), add it to _ALLOW "
          "in this file WITH the reason.")


def test_the_memory_store_is_the_layer_that_audits() -> None:
    """Specifically: the two functions whose silence started this."""
    src = (ROOT / "ava_bridge" / "memory_store.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef)}
    for name in ("delete_item", "delete_source"):
        assert name in fns, f"memory_store.{name} is gone — update this guard"
        assert _audits(fns[name]), (
            f"memory_store.{name}() no longer audits. docs/MEMORY.md promises "
            "deletions are logged; the store is the only layer that can keep that "
            "promise for every caller.")


def test_a_deletion_record_can_prove_what_it_removed() -> None:
    """A digest, not the text: the content is gone, so the record cannot quote it.

    Without this a delete record says only "id 7 was removed", which cannot
    distinguish a real erasure from a no-op — and is exactly the gap the plan
    flagged for the voiceprint receipt too.
    """
    src = (ROOT / "ava_bridge" / "memory_store.py").read_text(encoding="utf-8")
    assert "digest" in src and "sha256" in src, (
        "memory_store's delete records carry no content digest, so destruction is "
        "asserted rather than evidenced. Hash before deleting — a digest proves "
        "what went without retaining it, and is not reversible into it.")
