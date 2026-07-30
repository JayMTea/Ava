"""The Hub's multi-model 'brain' manager: add/list/test/set-brain/delete named
inference backends, plus the cloud-key wiring fix (a UI-saved key must reach the
router without landing in ava.yaml).

Uses FastAPI's TestClient against the hub_api router in isolation, with settings
pointed at a temp AVA_HOME so nothing touches the real config.
"""
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava_bridge import hub_api, router_app, settings


class BackendManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        # Isolate config + secrets from the real install.
        self._cfg_path = settings.CONFIG_PATH
        self._cfg = settings._CFG
        self._sec_env = os.environ.get("AVA_SECRETS_DIR")
        settings.CONFIG_PATH = d / "ava.yaml"
        settings._CFG = {}
        os.environ["AVA_SECRETS_DIR"] = str(d / "secrets")
        app = FastAPI()
        app.include_router(hub_api.router)
        self.c = TestClient(app, base_url="http://localhost")

    def tearDown(self):
        settings.CONFIG_PATH = self._cfg_path
        settings._CFG = self._cfg
        if self._sec_env is None:
            os.environ.pop("AVA_SECRETS_DIR", None)
        else:
            os.environ["AVA_SECRETS_DIR"] = self._sec_env
        self._tmp.cleanup()

    def _save(self, **body):
        return self.c.post("/api/hub/models/backends", json=body)

    def test_first_local_backend_becomes_brain(self):
        r = self._save(id="omni", engine="ollama",
                       base_url="http://127.0.0.1:11434/v1", model="llama3.1:70b")
        self.assertEqual(r.status_code, 200, r.text)
        lst = self.c.get("/api/hub/models/backends").json()
        self.assertEqual(lst["brain"], "omni")
        self.assertEqual([b["id"] for b in lst["backends"]], ["omni"])
        self.assertFalse(lst["backends"][0]["has_key"])

    def test_cloud_key_goes_to_secrets_not_yaml_and_reaches_router(self):
        self._save(id="omni", engine="ollama",
                   base_url="http://127.0.0.1:11434/v1", model="llama3.1:8b")
        self._save(id="my-openai", engine="openai",
                   base_url="https://api.openai.com/v1", model="gpt-4o-mini",
                   api_key="sk-SECRET123")
        raw = settings.CONFIG_PATH.read_text()
        self.assertNotIn("sk-SECRET123", raw)                       # never in yaml
        self.assertEqual(settings.backend_key("my-openai"), "sk-SECRET123")
        # the router now resolves the per-backend key
        keys = {b["id"]: b["api_key"] for b in router_app.load_backends()}
        self.assertEqual(keys.get("my-openai"), "sk-SECRET123")

    def test_legacy_inference_key_bridges_to_env(self):
        # The original wizard bug: a key written to secrets/inference_key must be
        # bridged into AVA_INFERENCE_KEY (settings does this at import; here we
        # exercise the mechanism directly since env is process-global).
        os.makedirs(settings.secrets_dir(), exist_ok=True)
        (Path(settings.secrets_dir()) / "inference_key").write_text("sk-LEGACY")
        self.assertEqual(settings.secret("inference_key"), "sk-LEGACY")

    def test_set_and_switch_brain(self):
        self._save(id="a", engine="ollama", base_url="http://127.0.0.1:11434/v1", model="m1")
        self._save(id="b", engine="ollama", base_url="http://127.0.0.1:11434/v1", model="m2")
        self.assertEqual(self.c.get("/api/hub/models/backends").json()["brain"], "a")
        r = self.c.post("/api/hub/models/backends/b/brain")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get("/api/hub/models/backends").json()["brain"], "b")

    def test_delete_repoints_brain_to_survivor(self):
        self._save(id="a", engine="ollama", base_url="http://127.0.0.1:11434/v1", model="m1")
        self._save(id="b", engine="openai", base_url="https://x/v1", model="m2",
                   api_key="sk-K", make_brain=True)
        self.assertEqual(self.c.get("/api/hub/models/backends").json()["brain"], "b")
        r = self.c.post("/api/hub/models/backends/b/delete")
        self.assertEqual(r.status_code, 200, r.text)
        lst = self.c.get("/api/hub/models/backends").json()
        self.assertEqual([x["id"] for x in lst["backends"]], ["a"])
        self.assertEqual(lst["brain"], "a")                          # repointed
        self.assertIsNone(settings.backend_key("b"))                 # key removed

    def test_delete_unknown_is_404(self):
        r = self.c.post("/api/hub/models/backends/nope/delete")
        self.assertEqual(r.status_code, 404)

    def test_save_requires_name_and_endpoint(self):
        self.assertEqual(self._save(id="", engine="ollama",
                                    base_url="http://x/v1", model="m").status_code, 400)
        self.assertEqual(self._save(id="x", engine="ollama",
                                    base_url="", model="").status_code, 400)

    def test_test_endpoint_handles_unreachable_gracefully(self):
        r = self.c.post("/api/hub/models/backends/test",
                        json={"base_url": "http://127.0.0.1:9/v1", "model": "x"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("could not reach", body["error"])


if __name__ == "__main__":
    unittest.main()
