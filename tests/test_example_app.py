"""examples/hello-app is the SDK's front door, so it has to actually work.

docs/CONNECTOR_SDK.md calls it "the acceptance test for 'fork Ava, add your app,
zero source edits'" — and its `PAGE` string was full of `{{`/`}}` escaping for a
`.format()` call that does not exist anywhere in the file. So the CSS `:root`
block never registered and the inline `<script>` was a hard SyntaxError: the
first thing a forker is invited to run rendered an unstyled page whose button
did nothing. Nothing in tests/ or qa/ had ever fetched it.

Two assertions, deliberately: `node --check` is the real one, and the
brace count is the one that still fails on a machine with no Node — the same
pairing tests/test_connector_codegen.py uses.
"""
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-helloapp-test-"))

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "examples" / "hello-app" / "server.py"
MANIFEST = ROOT / "examples" / "hello-app" / "connector.yaml"


def _page_literal() -> str:
    src = APP.read_text(encoding="utf-8")
    m = re.search(r"PAGE\s*=\s*(?:r?\"\"\"|r?''')(.*?)(?:\"\"\"|''')", src, re.S)
    assert m, "examples/hello-app/server.py no longer defines a PAGE literal"
    return m.group(1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PageIsValidTests(unittest.TestCase):
    def test_the_page_is_not_escaped_for_a_format_call_that_never_happens(self):
        """Node-free, so this still catches the regression in a minimal image."""
        src = APP.read_text(encoding="utf-8")
        page = _page_literal()
        self.assertNotIn("{{", page, (
            "PAGE contains `{{`, which is str.format escaping — but nothing in "
            "server.py calls .format(), so those braces reach the browser "
            "verbatim and break both the CSS and the inline script"))
        self.assertEqual(src.count(".format("), 0, (
            "if PAGE is now actually .format()ted, this test needs rewriting "
            "rather than the escaping removing"))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_inline_script_parses(self):
        scripts = re.findall(r"<script>(.*?)</script>", _page_literal(), re.S)
        self.assertTrue(scripts, "hello-app has no inline script any more")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write("\n".join(scripts))
            path = fh.name
        try:
            r = subprocess.run(["node", "--check", path],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"hello-app JS is invalid:\n{r.stderr}")
        finally:
            os.unlink(path)


class ManifestIsValidTests(unittest.TestCase):
    def test_the_shipped_example_passes_the_loader(self):
        """The example a forker copies must never be the thing that trips the
        manifest validator."""
        import yaml

        from ava_bridge import connectors
        m = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        errors: list = []
        connectors._validate(m, str(MANIFEST), errors)
        hard = [e for e in errors if e.get("severity") != "warn"]
        self.assertFalse(hard, f"examples/hello-app/connector.yaml is invalid: {hard}")


class ItActuallyServesTests(unittest.TestCase):
    """Boot it the way the docs tell a forker to, and speak its own protocol."""

    proc = None
    port = 0

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        env = {**os.environ, "HELLO_APP_PORT": str(cls.port), "PORT": str(cls.port)}
        cls.proc = subprocess.Popen([sys.executable, str(APP)], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True)
        deadline = time.time() + 20
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out = cls.proc.stdout.read() if cls.proc.stdout else ""
                raise AssertionError(f"hello-app exited at startup:\n{out[-2000:]}")
            try:
                urllib.request.urlopen(cls._url("/health"), timeout=1).read()
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        raise AssertionError("hello-app never became reachable")

    @classmethod
    def tearDownClass(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            cls.proc.wait(timeout=5)

    @classmethod
    def _url(cls, path):
        return f"http://127.0.0.1:{cls.port}{path}"

    def _get(self, path):
        with urllib.request.urlopen(self._url(path), timeout=5) as r:
            return r.status, r.read().decode()

    def test_health_answers(self):
        status, _ = self._get("/health")
        self.assertEqual(status, 200)

    def test_it_declares_its_tools(self):
        _s, body = self._get("/tools")
        data = json.loads(body)
        tools = data.get("tools") if isinstance(data, dict) else data
        self.assertTrue(tools, "/tools declared nothing — there is no app to connect")

    def test_a_tool_call_round_trips(self):
        _s, body = self._get("/tools")
        data = json.loads(body)
        tools = data.get("tools") if isinstance(data, dict) else data
        name = tools[0]["name"]
        # ava-tools/1 keys the call by `name`, not `tool` — docs/CONNECTOR_SDK.md §5.
        req = urllib.request.Request(
            self._url("/call"), method="POST",
            data=json.dumps({"name": name, "arguments": {}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text", json.loads(r.read()))

    def test_an_unknown_tool_is_a_404(self):
        req = urllib.request.Request(
            self._url("/call"), method="POST",
            data=json.dumps({"name": "no_such_tool", "arguments": {}}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 404)

    def test_the_served_page_has_no_stray_braces(self):
        """The end-to-end version of the first test: what the BROWSER receives."""
        _s, body = self._get("/")
        self.assertNotIn("{{", body)
        self.assertIn("<script>", body)


if __name__ == "__main__":
    unittest.main()
