"""The .mjs the connector generators emit must be valid JavaScript.

These files are Python f-strings that produce JS, and they run inside the agent
sandbox — so a syntax error surfaces as "the tool didn't work", nowhere near the
manifest that caused it. Nothing on the host ever parses them.

The concrete bug this guards: descriptions were interpolated into single-quoted
JS literals with only the apostrophe hand-escaped
(`.replace("'", "\\\\'")`). A manifest whose `description:` used a YAML block
scalar therefore emitted a literal newline inside a single-quoted string —
an unterminated literal, and an .mjs that cannot parse. Backslashes (a Windows
path in a description) broke it the same way.

`tests/test_scaffold.py::ExpressSyntaxTests` already `node --check`s the *other*
generator; this extends the same idea to the connector one.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from ava_bridge import connectors

# Inputs that broke the hand-rolled escaping, plus the plain case.
NASTY = [
    ("plain", "a simple description"),
    ("apostrophe", "it's the user's app"),
    ("newline", "line one\nline two"),           # the reported break
    ("backslash", r"a windows path C:\temp\x"),  # the other break
    ("quotes", 'he said "hello" and \'bye\''),
    ("both", "line one\nwith 'quotes' and C:\\path"),
    ("unicode", "café — naïve … 🎉"),
    ("js_injection", "'; process.exit(1); //"),
]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class GeneratedMjsParses(unittest.TestCase):
    def _check(self, src: str, label: str) -> None:
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "t.mjs")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            r = subprocess.run(["node", "--check", path],
                               capture_output=True, text=True)
            self.assertEqual(
                r.returncode, 0,
                f"generated .mjs is not valid JS for the {label!r} case:\n"
                f"{r.stderr}\n--- source ---\n{src[:600]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_action_tool_survives_hostile_descriptions(self):
        for label, desc in NASTY:
            with self.subTest(case=label):
                src = connectors.render_tool(
                    "demo", {"id": "act", "description": desc, "path": "/x"})
                self._check(src, label)

    def test_meta_tools_survive_hostile_labels(self):
        # find_tool / call embed the connector's *label*, which is equally
        # user-supplied and was escaped the same hand-rolled way.
        for label, text in NASTY:
            with self.subTest(case=label):
                original = connectors.load
                connectors.load = lambda: [{"id": "demo", "label": text}]  # noqa: E731
                try:
                    self._check(connectors.render_find_tool("demo"), f"find/{label}")
                    self._check(connectors.render_call_tool("demo"), f"call/{label}")
                finally:
                    connectors.load = original


class DescriptionIsNotHandEscaped(unittest.TestCase):
    """Runs without node, so CI catches a regression even on a minimal image."""

    def test_newline_description_is_escaped_not_literal(self):
        src = connectors.render_tool(
            "demo", {"id": "act", "description": "line one\nline two", "path": "/x"})
        line = next(ln for ln in src.splitlines() if ln.strip().startswith("description:"))
        # The newline must be the two characters \ and n, not an actual break.
        self.assertIn("\\n", line)
        self.assertIn("line two", line)


if __name__ == "__main__":
    unittest.main()
