"""Convention guard: an allocation test must never write into the real ledger.

This one is retrospective. The watchdog tests set `AVA_HOME` to a temp dir at import,
which looks sufficient — but `settings` freezes `AVA_HOME` on FIRST import, so in a
shared pytest process where another module imported it earlier, the env var is a silent
no-op. `run_cycle()` calls `breaker.note_ready()`, which writes to the ledger, and the
fixture model names `voice`, `ok`, `x`, `gone`, `c`, `obs`, `big`, `m` duly turned up in
the live box's `run/alloc/breaker.json`.

Harmless in that instance (every entry had zero failures), but the same leak could have
opened a breaker against a real model, or written a learned memory baseline from faked
readings — and it is exactly the hazard `tests/README.md` documents as "the commit
8d67dc3 bug".

The fix is the repo's own path-seam pattern: patch the accessor, not the environment. So
this asserts that any allocation test touching a state-writing module also redirects the
path it writes to. Style follows test_feature_convention.py — a static scan over tracked
sources that runs anywhere.
"""
import pathlib
import re
import unittest
from gitfiles import tracked_paths as _tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Modules whose functions write under $AVA_HOME, and the seam that redirects each.
WRITERS = {
    "breaker": ("ledger", '_dir'),      # breaker.json lives in the ledger dir
    "broker": ("ledger", '_dir'),       # lease records + model state
    "ledger": ("ledger", '_dir'),
    "watch": ("ledger", '_dir'),        # via breaker.note_ready()
}
# capacity writes only the learned baseline, and has its own path accessor.
BASELINE_SEAM = "_baseline_path"
# The broker's decision log lives under logs/, NOT the ledger dir — so redirecting the
# ledger does not cover it, and 91 fixture decisions duly landed in the live box's
# logs/alloc.jsonl next to the 2 real ones. Its own seam, patched the same way.
LOG_SEAM = "_log_path"
# Calls that reach _record(), and therefore the decision log. Anchored on a preceding
# `.` or start-of-call so a *test method* named `..._no_acquire(self)` is not mistaken
# for a call to `_acquire(...)`; restore_due/restore_now are absent deliberately, as
# they record to the audit trail rather than the decision log.
DECIDERS = re.compile(r"\.(?:acquire_api|_acquire|lease)\(|\bacquire_api\(")

_FIX = (
    "\n\nRedirect the path, not just the environment:\n"
    "  from ava_bridge.alloc import ledger\n"
    "  p = mock.patch.object(ledger, '_dir', lambda: self.tmpdir)\n"
    "  p.start(); self.addCleanup(p.stop)\n"
    "\nand for the learned baseline:\n"
    "  mock.patch.object(capacity, '_baseline_path',\n"
    "                    lambda: os.path.join(self.tmpdir, 'baseline.json'))\n"
    "\nWhy: `settings` freezes AVA_HOME at first import, so setting the env var is a\n"
    "silent no-op when another test module imported settings first — and the write\n"
    "lands in the real ledger. See tests/README.md 'Isolation patterns'."
)




class LedgerIsolationTests(unittest.TestCase):
    def test_alloc_tests_redirect_the_ledger_they_write_to(self):
        offenders = []
        for path in _tracked("tests/test_alloc*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel.endswith("test_alloc_isolation.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            # Which state-writing modules does this file actually exercise? The dot must
            # be followed by an identifier, or prose ("...to the audit ledger. Caught
            # if...") reads as an attribute access and the guard cries wolf.
            touched = {name for name in WRITERS
                       if re.search(rf"\b{name}\.\w", text)
                       or re.search(rf"^\s*(from|import) .*\b{name}\b", text,
                                    re.MULTILINE)}
            if not touched:
                continue
            # Does it redirect the ledger path seam anywhere?
            redirects = re.search(r"patch\.object\(\s*ledger\s*,\s*['\"]_dir['\"]", text)
            if not redirects:
                offenders.append(f"{rel}: exercises {sorted(touched)} "
                                 "without patching ledger._dir")
        self.assertFalse(offenders, "allocation tests may write into the real ledger:\n  "
                         + "\n  ".join(offenders) + _FIX)

    def test_tests_that_decide_redirect_the_decision_log(self):
        offenders = []
        for path in _tracked("tests/test_alloc*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel.endswith("test_alloc_isolation.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not DECIDERS.search(text):
                continue                    # nothing here reaches _record()
            if LOG_SEAM not in text:
                offenders.append(f"{rel}: makes lease decisions without patching "
                                 f"broker.{LOG_SEAM} — they land in logs/alloc.jsonl")
        self.assertFalse(offenders, "fixture decisions may pollute the log an operator "
                         "reads to enable enforcement:\n  " + "\n  ".join(offenders))

    def test_tests_that_start_releases_join_them_before_teardown(self):
        # Patching the seam is not enough when the write happens on another thread. A
        # deferred acquire returns immediately and releases in the background, so a test
        # that ends without joining lets that thread run on past `p.stop()` — and its
        # next ledger write lands in the real one. Redirect the path AND own the thread.
        offenders = []
        for path in _tracked("tests/test_alloc*.py"):
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            if "wait=False" not in text and "_spawn_actuation" not in text:
                continue                    # nothing here starts a release thread
            if "wait_for_actuations" not in text:
                offenders.append(f"{rel}: starts a background release without joining "
                                 "it (broker.wait_for_actuations)")
        self.assertFalse(offenders, "\n  ".join(offenders) + _FIX)

    def test_capacity_tests_redirect_the_learned_baseline(self):
        offenders = []
        for path in _tracked("tests/test_alloc*.py"):
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            # Only files that actually drive baseline learning need the seam.
            if "baseline_gib(" not in text:
                continue
            if BASELINE_SEAM not in text:
                offenders.append(f"{rel}: calls baseline_gib() without patching "
                                 f"capacity.{BASELINE_SEAM}")
        self.assertFalse(offenders, "\n  ".join(offenders) + _FIX)


if __name__ == "__main__":
    unittest.main()
