"""Embedded-router starter tests (no real server; uvicorn is stubbed)."""
import unittest
from unittest import mock

from ava_bridge import router_host


class StartEmbeddedTests(unittest.TestCase):
    def setUp(self):
        router_host._state.update(status="idle", thread=None, detail=None)

    def test_disabled_by_config(self):
        with mock.patch.dict("os.environ", {"AVA_ROUTER_EMBEDDED": "false"}):
            self.assertEqual(router_host.start_embedded(), "disabled")
        self.assertEqual(router_host._state["status"], "disabled")

    def test_yields_to_external_router(self):
        with mock.patch.dict("os.environ", {"AVA_ROUTER_EMBEDDED": "auto"}), \
                mock.patch.object(router_host, "_external_alive",
                                  return_value=True):
            self.assertEqual(router_host.start_embedded(), "external")
        self.assertEqual(router_host._state["status"], "external")

    def test_starts_daemon_thread_when_port_free(self):
        ran = {}

        class FakeServer:
            def __init__(self, config):
                ran["config"] = config

            def run(self):
                ran["ran"] = True

        with mock.patch.dict("os.environ", {"AVA_ROUTER_EMBEDDED": "auto"}), \
                mock.patch.object(router_host, "_external_alive",
                                  return_value=False), \
                mock.patch("uvicorn.Server", FakeServer):
            self.assertEqual(router_host.start_embedded(), "embedded")
        self.assertEqual(router_host._state["status"], "embedded")
        router_host._state["thread"].join(timeout=5)
        self.assertTrue(ran.get("ran"))
        self.assertEqual(ran["config"].port,
                         router_host._cfg()[1])


if __name__ == "__main__":
    unittest.main()
