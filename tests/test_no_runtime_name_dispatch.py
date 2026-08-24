"""Nothing outside the runtime package may reach for a runtime BY NAME.

`ava doctor` and `ava agent status` both called `runtime.nemoclaw().status()`
outright. On a box running the gateway runtime that reported a DIFFERENT
runtime's facts than the one actually serving turns — and reported them as if
they were live, so the gateway's own state (ready? which version? why not?) was
invisible on exactly the box where it mattered.

The seam already answers this: `runtime.gate()` returns what is serving, every
runtime's `status()` carries the same core keys, and `capabilities()` says what
it can do. A name lookup is how a fifth runtime ends up needing edits in places
that have nothing to do with it.

House style is tests/test_diagram_sync.py — a static scan over `git ls-files`,
failing with instructions.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The accessor functions on `ava_bridge.runtime`, one per adapter module.
NAMED = ("direct", "nemoclaw", "openclaw_gw", "remote")
CALL = re.compile(r"\bruntime\.(" + "|".join(NAMED) + r")\s*\(")

# The runtime package defines and wires these, so it is allowed to name them.
# `provision.py` is allowed because provisioning IS runtime-specific work.
ALLOW_PREFIXES = (
    os.path.join("ava_bridge", "runtime") + os.sep,
    os.path.join("tests") + os.sep,
    os.path.join("qa") + os.sep,
)


# Call sites that legitimately name a runtime, with the reason. Naming one is
# only a bug when it is standing in for "whatever is serving" — a function whose
# whole purpose IS a specific runtime is not dispatching, it is being explicit.
ALLOW = {
    "ava_bridge/agent.py:runtime.direct()":
        "chat_direct() is the degraded floor BY DEFINITION — it is the "
        "fallback callers ask for by name when the full agent is unavailable, "
        "so resolving it through gate() would defeat the point.",
}


def _sources():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    for rel in out.split():
        if not rel.endswith(".py"):
            continue
        if rel.startswith(ALLOW_PREFIXES):
            continue
        yield rel


class NameDispatchTests(unittest.TestCase):

    def test_no_module_picks_a_runtime_by_name(self):
        bad = []
        scanned = 0
        for rel in _sources():
            path = os.path.join(ROOT, rel)
            try:
                with open(path, encoding="utf-8") as f:
                    body = f.read()
            except FileNotFoundError:
                self.fail(f"{rel} is tracked by git but missing from disk — "
                          "stage the deletion so the scan and the tree agree")
            scanned += 1
            for m in CALL.finditer(body):
                # A COMMENT explaining the old bug is not a call site.
                line_start = body.rfind("\n", 0, m.start()) + 1
                line = body[line_start:body.find("\n", m.start())]
                if line.lstrip().startswith("#"):
                    continue
                if f"{rel}:runtime.{m.group(1)}()" in ALLOW:
                    continue
                bad.append(f"{rel}:{body[:m.start()].count(chr(10)) + 1} "
                           f"calls runtime.{m.group(1)}()")
        self.assertGreater(scanned, 10,
                           "scanned almost nothing — the file list has drifted "
                           "and this guard is passing vacuously")
        self.assertEqual(
            bad, [],
            "a runtime was selected by NAME instead of through the seam. Use "
            "`runtime.gate()` for what is serving, `runtime.configured()` for "
            "what is configured, and read `status()`/`capabilities()` rather "
            "than assuming which adapter answered:\n  " + "\n  ".join(bad))

    def test_every_allowance_still_has_a_call_site(self):
        """A ratchet. When an allowed call goes away the entry must go with it,
        or the list quietly becomes a place to hide the next one."""
        stale = []
        for entry in ALLOW:
            rel, _, call = entry.partition(":")
            path = os.path.join(ROOT, rel)
            body = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            if call not in body:
                stale.append(entry)
        self.assertEqual(stale, [],
                         "allowed call sites that no longer exist — remove "
                         f"them from ALLOW: {stale}")

    def test_every_allowance_states_a_reason(self):
        for entry, why in ALLOW.items():
            self.assertGreater(len(why), 60,
                               f"{entry} is allowed without explaining why")

class NameComparisonTests(unittest.TestCase):
    """Comparing `rt.name` to a literal is name-dispatch wearing a disguise.

    The guard above catches `runtime.nemoclaw()`. It did NOT catch
    `rt.name != "direct"` — which is the same decision made the same way, and
    which survived in `models.py` long enough to be found by re-reading an
    audit rather than by the guard written to prevent it. Both forms mean "this
    file knows the list of runtimes", which is what makes adding a fifth one an
    edit in places that have nothing to do with it.

    Ask a capability instead: `is_local()`, `supports_abort()`, `capabilities()`,
    or simply whether the method you need is there.
    """

    def _offenders(self, rel: str, body: str) -> list[str]:
        """Comparisons found by PARSING, not by matching text.

        A regex cannot tell `rt.name == "direct"` in code from the same words
        inside a docstring explaining why that was once a bug — and this file
        contains exactly that prose. `ast` reads the code and nothing else.
        """
        import ast
        try:
            tree = ast.parse(body)
        except SyntaxError:
            return []
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            if not (isinstance(left, ast.Attribute) and left.attr == "name"):
                continue
            for op, comp in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.Eq, ast.NotEq)):
                    continue
                if isinstance(comp, ast.Constant) and comp.value in NAMED:
                    out.append(f"{rel}:{node.lineno} compares name to "
                               f"'{comp.value}'")
        return out

    def test_no_module_compares_a_runtime_name_to_a_literal(self):
        bad, scanned = [], 0
        for rel in _sources():
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    body = f.read()
            except FileNotFoundError:
                self.fail(f"{rel} is tracked by git but missing from disk")
            scanned += 1
            bad += self._offenders(rel, body)
        self.assertGreater(scanned, 10, "scanned almost nothing")
        self.assertEqual(
            bad, [],
            "a runtime was identified by comparing its NAME. Ask what it can "
            "DO instead — is_local(), is_floor(), capabilities(), or whether "
            "the method you need exists:\n  " + "\n  ".join(bad))

    def test_the_parse_actually_finds_this_pattern(self):
        """Anti-vacuous: an ast walk that matches nothing would pass silently
        forever, which is worse than the regex it replaced."""
        got = self._offenders("x.py", 'if rt.name == "direct":\n    pass\n')
        self.assertEqual(len(got), 1, got)

    def test_the_parse_ignores_the_same_words_in_prose(self):
        """The false positive that motivated the rewrite: phone_bridge.py's
        docstring explains a bug BY QUOTING the comparison that caused it."""
        src = '"""We used to gate on rt.name == \'nemoclaw\' here."""\npass\n'
        self.assertEqual(self._offenders("x.py", src), [])


class PunchThroughTests(unittest.TestCase):
    """Nothing outside the runtime package may reach into `rt._client`.

    Four bridge routes decided whether a runtime had a control plane by asking
    `getattr(rt, "_client", None)` — testing for a PRIVATE ATTRIBUTE's absence.
    That silently answers "no gateway" for any adapter that names its client
    something else, and couples the relay to one adapter's internals. The seam
    answers it: `rt.control_plane()` returns the client or None.
    """

    REACH = re.compile(r"""(?:getattr\(\s*\w+\s*,\s*["']_client["']|\b\w+\._client\b)""")

    def test_no_module_reaches_into_a_runtimes_client(self):
        bad, scanned = [], 0
        for rel in _sources():
            path = os.path.join(ROOT, rel)
            try:
                with open(path, encoding="utf-8") as f:
                    body = f.read()
            except FileNotFoundError:
                self.fail(f"{rel} is tracked by git but missing from disk")
            scanned += 1
            for m in self.REACH.finditer(body):
                line_start = body.rfind("\n", 0, m.start()) + 1
                line = body[line_start:body.find("\n", m.start())]
                if line.lstrip().startswith("#"):
                    continue
                bad.append(f"{rel}:{body[:m.start()].count(chr(10)) + 1}")
        self.assertGreater(scanned, 10, "scanned almost nothing")
        self.assertEqual(
            bad, [],
            "reached into a runtime's private client. Use "
            "`rt.control_plane()`, which returns the client or None and is the "
            "one honest answer to 'does this runtime have a gateway':\n  "
            + "\n  ".join(bad))

    def test_the_seam_member_exists(self):
        from ava_bridge.runtime.base import AgentRuntime
        self.assertIsNone(AgentRuntime.control_plane(object()),
                          "the default must be None — a runtime with no "
                          "control plane has none, and that is not an error")


class SeamTests(unittest.TestCase):

    def test_the_seam_still_offers_what_this_guard_redirects_to(self):
        """Named so the failure above cannot point at something that is gone."""
        from ava_bridge import runtime
        for fn in ("gate", "configured"):
            self.assertTrue(callable(getattr(runtime, fn, None)),
                            f"runtime.{fn}() is what this guard tells people "
                            "to use instead — it must exist")


if __name__ == "__main__":
    unittest.main()
