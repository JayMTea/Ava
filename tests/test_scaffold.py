"""`ava app new`: the generated surface must CONFORM to ava-tools/1, not just exist."""
import importlib.util
import os
import py_compile
import shutil
import subprocess
import tempfile
import unittest

import yaml

from ava_bridge import scaffold


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_bad_id_rejected(self):
        for bad in ("X", "1app", "has space", "-lead"):
            with self.assertRaises(ValueError):
                scaffold.create(bad, out_dir=self.tmp)

    def test_refuses_overwrite(self):
        scaffold.create("myapp", out_dir=self.tmp)
        with self.assertRaises(FileExistsError):
            scaffold.create("myapp", out_dir=self.tmp)

    def test_every_framework_generates_valid_files(self):
        for fw in scaffold.FRAMEWORKS:
            d = os.path.join(self.tmp, fw)
            written = scaffold.create("myapp", framework=fw, port=9123,
                                      out_dir=d, ui=(fw == "fastapi"))
            names = {os.path.relpath(p, d) for p in written}
            self.assertIn("ava/README-AVA.md", names)
            self.assertIn("ava/connector.yaml", names)
            # the manifest parses and points discover at the right base
            with open(os.path.join(d, "ava/connector.yaml"), encoding="utf-8") as f:
                m = yaml.safe_load(f)
            self.assertEqual(m["id"], "myapp")
            self.assertEqual(m["actions"]["discover"]["base"], "http://127.0.0.1:9123")
            self.assertEqual(m["actions"]["discover"]["list"], "/tools")
            if fw == "fastapi":
                self.assertEqual(m["ui"]["embed"], "iframe")   # --ui was on
            else:
                self.assertNotIn("ui", m)                       # commented out
            # python surfaces compile; the express one is checked by node below
            surf = os.path.join(d, "ava", "surface.mjs" if fw == "express" else "surface.py")
            self.assertTrue(os.path.exists(surf))
            if surf.endswith(".py"):
                py_compile.compile(surf, doraise=True)


def _load_surface(path):
    spec = importlib.util.spec_from_file_location("gen_surface_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FastapiConformanceTests(unittest.TestCase):
    """Mount the generated FastAPI surface and hit it — the acceptance test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        scaffold.create("myapp", framework="fastapi", out_dir=self.tmp)
        self.mod = _load_surface(os.path.join(self.tmp, "ava", "surface.py"))
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        app = FastAPI()
        self.mod.register(app)
        # /tools and /call are credentialed and fail closed, so the conformance
        # client has to present a token like Ava does. The refusal path itself
        # is covered in tests/test_scaffold_auth.py.
        os.environ["MYAPP_TOKEN"] = "conformance-token"
        self.addCleanup(os.environ.pop, "MYAPP_TOKEN", None)
        self.client = TestClient(
            app, headers={"Authorization": "Bearer conformance-token"}, base_url="http://localhost")

    def test_tools_listing_conforms(self):
        r = self.client.get("/tools").json()
        self.assertEqual(r["facade"], "ava-tools/1")
        ping = next(t for t in r["tools"] if t["name"] == "ping")
        self.assertEqual(ping["access"], "read")
        self.assertNotIn("handler", ping)

    def test_well_known_self_description(self):
        r = self.client.get("/.well-known/ava.json").json()
        self.assertEqual(r["facade"], "ava-tools/1")
        self.assertEqual(r["label"], "Myapp")
        self.assertEqual((r["tools"], r["call"]), ("/tools", "/call"))

    def test_call_contract(self):
        ok = self.client.post("/call", json={"name": "ping", "arguments": {}})
        self.assertEqual((ok.status_code, ok.json()), (200, {"pong": True}))
        missing = self.client.post("/call", json={"name": "nope"})
        self.assertEqual(missing.status_code, 404)
        self.assertIn("unknown tool", missing.json()["error"])

    def test_required_args_and_crash_contract(self):
        @self.mod.tool("boom", "test tool",
                       {"x": {"type": "string"}}, required=["x"])
        def _boom(args):
            raise RuntimeError("kaboom")
        r = self.client.post("/call", json={"name": "boom", "arguments": {}})
        self.assertEqual(r.status_code, 400)                 # missing required
        self.assertIn("missing x", r.json()["error"])
        r = self.client.post("/call", json={"name": "boom", "arguments": {"x": "1"}})
        self.assertEqual(r.status_code, 500)                 # crash -> message
        self.assertEqual(r.json()["error"], "kaboom")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ExpressSyntaxTests(unittest.TestCase):
    def test_generated_mjs_parses(self):
        tmp = tempfile.mkdtemp()
        scaffold.create("myapp", framework="express", out_dir=tmp)
        subprocess.run(["node", "--check", os.path.join(tmp, "ava", "surface.mjs")],
                       check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
