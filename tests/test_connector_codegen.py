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
                connectors.load = lambda: [{"id": "demo", "label": text}]
                try:
                    self._check(connectors.render_find_tool("demo"), f"find/{label}")
                    self._check(connectors.render_call_tool("demo"), f"call/{label}")
                finally:
                    connectors.load = original


def _every_generated_source() -> list:
    """(label, source) for each generator shape: static action, find, call."""
    original = connectors.load
    connectors.load = lambda: [{"id": "demo", "label": "Demo"}]
    try:
        return [
            ("static", connectors.render_tool(
                "demo", {"id": "act", "description": "x", "path": "/x"})),
            ("find", connectors.render_find_tool("demo")),
            ("call", connectors.render_call_tool("demo")),
        ]
    finally:
        connectors.load = original


class ReachesTheBridgeThroughTheGuardProxy(unittest.TestCase):
    """Every generated tool must call the bridge the way core tools do.

    The sandbox's network guard admits outbound traffic only through its L7
    proxy: a direct connection to the bridge is refused (curl reports 000, Node
    reports ECONNREFUSED — measured from inside the sandbox). Core tools go
    through `ctx.http` (`_lib.mjs`: curl with `--proxy`, `direct: false`); the
    generated connector tools used a bare `fetch()`, which ignores the proxy —
    so every connector call the agent ever made failed with "fetch failed",
    while the identical request from a shell (proxy env set) succeeded. The
    defect was invisible to every host-side test because nothing on the host
    executes these files.
    """

    def test_no_generated_tool_uses_a_bare_fetch(self):
        for label, src in _every_generated_source():
            with self.subTest(shape=label):
                self.assertNotIn("fetch(", src)
                self.assertIn("ctx.http.", src)
                self.assertIn("direct: false", src)
                self.assertIn("'X-Ava-Internal-Token': ctx.internalToken", src)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_generated_handlers_drive_ctx_http_with_the_bridge_route(self):
        """Execute each handler under node with a recording ctx.http — the
        contract the real `_lib.mjs` exposes — and check what it was asked."""
        harness = """
import tool from './t.mjs';
const calls = [];
const ctx = {
  internalToken: 'tok',
  http: {
    getJson: async (url, opts) => { calls.push({m: 'GET', url, opts}); return {ok: 1}; },
    postJson: async (url, body, opts) => { calls.push({m: 'POST', url, body, opts}); return {ok: 1}; },
  },
};
const out = await tool.handler({query: 'q w', limit: 3, name: 'do_it', arguments: {a: 1}, x: 2}, ctx);
console.log(JSON.stringify({calls, out}));
"""
        expect = {
            "static": ("POST", "/internal/connector/demo/act"),
            "find": ("GET", "/internal/connector/demo/__tools?q=q%20w&limit=3"),
            "call": ("POST", "/internal/connector/demo/__call"),
        }
        for label, src in _every_generated_source():
            with self.subTest(shape=label):
                tmp = tempfile.mkdtemp()
                try:
                    with open(os.path.join(tmp, "t.mjs"), "w", encoding="utf-8") as f:
                        f.write(src)
                    with open(os.path.join(tmp, "run.mjs"), "w", encoding="utf-8") as f:
                        f.write(harness)
                    r = subprocess.run(["node", "run.mjs"], cwd=tmp,
                                       capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    import json
                    got = json.loads(r.stdout.strip().splitlines()[-1])
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                self.assertEqual(len(got["calls"]), 1, got)
                call = got["calls"][0]
                method, path = expect[label]
                self.assertEqual(call["m"], method)
                self.assertTrue(call["url"].endswith(path), call["url"])
                self.assertIs(call["opts"]["direct"], False)
                self.assertEqual(call["opts"]["headers"]["X-Ava-Internal-Token"], "tok")
                if label == "call":
                    self.assertEqual(call["body"], {"name": "do_it", "arguments": {"a": 1}})
                if label == "static":
                    self.assertEqual(call["body"]["x"], 2)
                self.assertIn('"ok": 1', got["out"])


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
