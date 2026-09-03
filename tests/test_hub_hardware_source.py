"""Setup -> Hardware -> "Where the hardware is" (ava_bridge/hub/hardware.py).

The form that names another machine's exporters. Three properties matter: a
save is validated and refused rather than silently corrected, an env override
is reported rather than overwritten, and the GET tells the owner whether the
addresses ANSWER - in the registry's vocabulary - so "I typed it and nothing
changed" has a visible cause.

Minimal app with just the hardware router mounted (the repo's "minimal apps over
the real app" pattern): no bridge, no auth middleware, no network.
Run: python -m pytest tests/test_hub_hardware_source.py -q
"""
import os
import unittest
from unittest import mock

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava_bridge import features, hwexporters, hwinfo, settings
from ava_bridge.hub import hardware as hub

app = FastAPI()
app.include_router(hub.router, prefix="/api/hub")

NODE_URL = "http://gpu-box:9100/metrics"
NODE_PAGE = (
    "node_memory_MemTotal_bytes 68719476736\n"
    "node_memory_MemAvailable_bytes 34359738368\n"
    'node_uname_info{nodename="gpu-box"} 1\n'
)


class SourceRouteTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        self.cfg = {"label": "", "node_url": "", "gpu_url": "",
                    "disk_mount": "/", "timeout_s": 1.5}
        self.on = True
        self.saved: list[dict] = []
        self.pages: dict[str, tuple[int, str]] = {}

        def fake_get(url, timeout):
            if url not in self.pages:
                raise requests.exceptions.ConnectionError(url)
            return self.pages[url]

        real_enabled = features.enabled

        def fake_enabled(key):
            return self.on if key == hwexporters.KEY else real_enabled(key)

        for p in (mock.patch.object(hwexporters, "_http_get", side_effect=fake_get),
                  mock.patch.object(hwexporters, "config", side_effect=lambda: dict(self.cfg)),
                  mock.patch.object(features, "enabled", side_effect=fake_enabled),
                  mock.patch.object(settings, "save_patch",
                                    side_effect=lambda patch: self.saved.append(patch) or {}),
                  mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        for var in (hwexporters.NODE_URL_ENV, hwexporters.GPU_URL_ENV,
                    hwexporters.MOUNT_ENV, hwexporters.LABEL_ENV):
            os.environ.pop(var, None)
        hwinfo.reset_cache()
        self.addCleanup(hwinfo.reset_cache)

    # --- GET ------------------------------------------------------------------ #
    def test_unconfigured_reads_as_local_and_editable(self):
        r = self.c.get("/api/hub/hardware/source").json()
        self.assertFalse(r["configured"])
        self.assertEqual(r["state"], "unconfigured")
        self.assertTrue(r["editable"])
        self.assertEqual(r["error_code"], "")

    def test_configured_and_answering_shows_a_taste_of_the_reading(self):
        self.cfg["node_url"] = NODE_URL
        self.pages[NODE_URL] = (200, NODE_PAGE)
        r = self.c.get("/api/hub/hardware/source").json()
        self.assertEqual((r["state"], r["reachable"], r["error_code"]), ("ok", True, ""))
        self.assertEqual(r["resolved_label"], "gpu-box")
        self.assertEqual(r["mem_total_gb"], 64.0)
        self.assertIsNone(r["gpu_name"])

    def test_configured_and_silent_carries_the_registry_code(self):
        self.cfg["node_url"] = NODE_URL
        r = self.c.get("/api/hub/hardware/source").json()
        self.assertEqual((r["state"], r["reachable"]), ("down", False))
        self.assertEqual(r["error_code"], "remote_hardware_down")
        self.assertIn(NODE_URL, r["node_error"])

    def test_switched_off_is_reported_not_probed(self):
        self.on = False
        self.cfg["node_url"] = NODE_URL
        r = self.c.get("/api/hub/hardware/source").json()
        self.assertEqual((r["state"], r["enabled"], r["configured"]), ("off", False, True))
        self.assertEqual(r["error_code"], "remote_hardware_off")

    def test_env_override_is_named_and_makes_the_form_read_only(self):
        os.environ[hwexporters.GPU_URL_ENV] = "http://gpu-box:9400/metrics"
        r = self.c.get("/api/hub/hardware/source").json()
        self.assertFalse(r["editable"])
        self.assertEqual(r["env"]["gpu_url"], hwexporters.GPU_URL_ENV)
        self.assertEqual(r["env"]["node_url"], "")

    # --- POST ----------------------------------------------------------------- #
    def test_save_writes_the_block_and_needs_no_restart(self):
        body = {"label": "Workstation", "node_url": NODE_URL,
                "gpu_url": "http://gpu-box:9400/metrics", "disk_mount": "/data"}
        r = self.c.post("/api/hub/hardware/source", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["restart_required"])
        self.assertEqual(self.saved, [{"hardware": {"exporters": body}}])

    def test_clearing_everything_goes_back_to_this_machine(self):
        r = self.c.post("/api/hub/hardware/source",
                        json={"label": "", "node_url": "", "gpu_url": "", "disk_mount": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.saved[0]["hardware"]["exporters"],
                         {"label": "", "node_url": "", "gpu_url": "", "disk_mount": "/"})

    def test_bad_addresses_are_refused_not_corrected(self):
        for field, value, why in (("node_url", "gpu-box:9100", "http"),
                                  ("gpu_url", "ftp://gpu-box/metrics", "http"),
                                  ("disk_mount", "data", "absolute"),
                                  ("label", "x" * 61, "60")):
            r = self.c.post("/api/hub/hardware/source", json={field: value})
            self.assertEqual(r.status_code, 400, (field, r.text))
            self.assertEqual(r.json()["field"], field)
            self.assertIn(why, r.json()["error"])
        self.assertEqual(self.saved, [])

    def test_env_override_refuses_a_save(self):
        os.environ[hwexporters.NODE_URL_ENV] = NODE_URL
        r = self.c.post("/api/hub/hardware/source", json={"node_url": ""})
        self.assertEqual(r.status_code, 409)
        self.assertIn(hwexporters.NODE_URL_ENV, r.json()["error"])
        self.assertEqual(self.saved, [])

    def test_invalid_json_is_a_400(self):
        r = self.c.post("/api/hub/hardware/source", content=b"nope",
                        headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
